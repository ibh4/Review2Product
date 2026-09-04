"""Review2Product FastAPI 入口。

端点一览（OpenAPI 文档: /docs）：
  GET  /health
  GET  /api/products
  GET  /api/products/{id}
  GET  /api/products/{id}/reviews
  POST /api/analyze                     body: {"product_id": "..."}
  GET  /api/analysis/{product_id}       （缺失时自动触发分析）
  GET  /api/pain-points/{product_id}
  GET  /api/pain-points/{pain_id}/evidence   pain_id = "<product_id>::<cluster_id>"
  GET  /api/product-v2/{product_id}
  POST /api/generate-listing            body: {"product_id": "..."}
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.app import store
from backend.app.schemas import (AnalysisOut, HealthOut, ListingOut, PainPointOut,
                                 ProductSummary, ProductV2Out, ReviewOut, RootCauseOut,
                                 ProductParamOut, FAQItem)
from backend.services import analysis as analysis_svc
from backend.services import translations
from backend.services.llm import llm_mode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("r2p.api")


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        store.run_pipeline_if_needed()
    except Exception as e:
        log.error("startup pipeline failed（API 仍将尝试懒加载）：%s", e)
    yield


app = FastAPI(
    title="Review2Product API",
    description="Global Voice of Customer → Product Evolution Agent",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   "http://localhost:5174", "http://127.0.0.1:5174"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    product_id: str | None = None  # 缺省取 demo hero 商品


class TranslateRequest(BaseModel):
    texts: list[str]
    target: str = "zh-CN"


def _analysis_or_404(product_id: str) -> dict:
    art = store.load_analysis(product_id)
    if art is None:
        df = store.load_clean_df()
        if product_id not in set(df["product_id"]):
            raise HTTPException(404, f"product {product_id} not found")
        log.info("商品 %s 无分析产物，自动触发分析", product_id)
        art = analysis_svc.analyze_product(df, product_id)
        analysis_svc.save_artifact(art)
        store.save_hero(product_id)
    return art


def _to_pain_out(art: dict) -> list[PainPointOut]:
    out = []
    for p in art["pain_points"]:
        d = dict(p)
        d["pain_point_id"] = f"{art['product_id']}::{p['cluster_id']}"
        d.setdefault("frequency", 0.0)
        d.setdefault("helpfulness", 0.0)
        d.setdefault("recency", 0.0)
        d.setdefault("score_components", {})
        out.append(PainPointOut(**d))
    return out


@app.get("/health", response_model=HealthOut)
def health():
    try:
        products = store.list_products()
        reviews = int(sum(p["review_count"] for p in products))
        ready = any(p["analyzed"] for p in products)
        src = products[0]["data_source"] if products else "none"
        return HealthOut(status="ok", llm_mode=llm_mode(), data_source=src,
                         products=len(products), reviews=reviews, analysis_ready=ready)
    except Exception as e:
        log.exception("health check failed")
        raise HTTPException(500, f"unhealthy: {e}")


@app.get("/api/products", response_model=list[ProductSummary])
def products():
    return [ProductSummary(**{**p, "product_title_zh": translations.product_title_zh(p["product_id"])})
            for p in store.list_products()]


@app.get("/api/products/{product_id}", response_model=ProductSummary)
def product_detail(product_id: str):
    for p in store.list_products():
        if p["product_id"] == product_id:
            return ProductSummary(**{**p, "product_title_zh": translations.product_title_zh(product_id)})
    raise HTTPException(404, "product not found")


@app.post("/api/translate")
def translate(req: TranslateRequest):
    """英文评论/文本动态翻译（LLM 优先，gtx 兜底，失败位置返回 null）。"""
    texts = [t[:2000] for t in req.texts[:30]]  # 单条限长、批量限 30
    out = translations.translate_batch(texts, req.target)
    return {"translations": out}


@app.get("/api/products/{product_id}/timeseries")
def product_timeseries(product_id: str) -> list[dict]:
    """月度评论量/评分/负面数聚合（Review Dynamics 图数据源）。"""
    if product_id not in {p["product_id"] for p in store.list_products()}:
        raise HTTPException(404, "product not found")
    return store.get_timeseries(product_id)


@app.get("/api/products/{product_id}/reviews", response_model=list[ReviewOut])
def product_reviews(product_id: str, limit: int = Query(50, ge=1, le=500),
                    min_rating: float | None = Query(None, ge=1, le=5),
                    max_rating: float | None = Query(None, ge=1, le=5)):
    rows = store.get_reviews(product_id, limit=limit, min_rating=min_rating, max_rating=max_rating)
    if not rows and product_id not in {p["product_id"] for p in store.list_products()}:
        raise HTTPException(404, "product not found")
    return [ReviewOut(**r) for r in rows]


@app.post("/api/analyze", response_model=AnalysisOut)
def analyze(req: AnalyzeRequest):
    pid = req.product_id or (store._hero_product_id() or "")
    if not pid:
        prods = store.list_products()
        if not prods:
            raise HTTPException(503, "no data, run pipeline first")
        pid = prods[0]["product_id"]
    df = store.load_clean_df()
    if pid not in set(df["product_id"]):
        raise HTTPException(404, f"product {pid} not found")
    art = analysis_svc.analyze_product(df, pid)
    analysis_svc.save_artifact(art)
    store.save_hero(pid)
    return _render_analysis(art)


@app.get("/api/analysis/{product_id}", response_model=AnalysisOut)
def get_analysis(product_id: str):
    return _render_analysis(_analysis_or_404(product_id))


@app.get("/api/pain-points/{product_id}", response_model=list[PainPointOut])
def pain_points(product_id: str):
    return _to_pain_out(_analysis_or_404(product_id))


@app.get("/api/pain-points/{pain_id}/evidence", response_model=list[ReviewOut])
def pain_evidence(pain_id: str, limit: int = Query(50, ge=1, le=100)):
    if "::" not in pain_id:
        raise HTTPException(400, "pain_id format should be '<product_id>::<cluster_id>'")
    pid, cluster_id = pain_id.rsplit("::", 1)
    art = _analysis_or_404(pid)
    target = next((p for p in art["pain_points"] if str(p["cluster_id"]) == cluster_id), None)
    if target is None:
        raise HTTPException(404, f"pain point {pain_id} not found")
    ids = target.get("evidence_review_ids", [])[:limit]
    if not ids:
        return []
    rows = store.get_reviews(pid, limit=limit, review_ids=ids)
    order = {rid: i for i, rid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["review_id"], 999))
    for r in rows:
        r["matched_pain"] = target["name"]
    return [ReviewOut(**r) for r in rows]


@app.get("/api/product-v2/{product_id}", response_model=ProductV2Out)
def product_v2(product_id: str):
    return ProductV2Out(**_analysis_or_404(product_id)["product_v2"])


@app.post("/api/generate-listing", response_model=ListingOut)
def generate_listing(req: AnalyzeRequest):
    art = _analysis_or_404(req.product_id) if req.product_id else _analysis_or_404(
        store._hero_product_id() or store.list_products()[0]["product_id"])
    return ListingOut(**art["listing"])


def _render_analysis(art: dict) -> AnalysisOut:
    return AnalysisOut(
        product_id=art["product_id"], product_title=art["product_title"],
        product_title_zh=translations.product_title_zh(art["product_id"]),
        category=art["category"], data_source=art["data_source"],
        generated_at=art["generated_at"], llm_mode=art.get("llm_mode", "mock"),
        stats=art["stats"], pain_points=_to_pain_out(art),
        root_causes={k: RootCauseOut(**v) for k, v in art["root_causes"].items()},
        product_v2=ProductV2Out(
            positioning=art["product_v2"]["positioning"],
            parameters=[ProductParamOut(**p) for p in art["product_v2"]["parameters"]],
            selling_points=art["product_v2"]["selling_points"],
            before_after_profile=art["product_v2"]["before_after_profile"],
        ),
        listing=ListingOut(**{**art["listing"], "faq": [FAQItem(**f) for f in art["listing"]["faq"]]}),
    )
