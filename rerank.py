"""MiniLM re-ranker (Part 2 of the Piper-style fix).

Piper's retrieve-then-rerank pattern: keyword search pulls a shortlist
of candidates, then a small embedding model (all-MiniLM-L6-v2, ~23 MB
quantized ONNX) scores each candidate by semantic similarity to the
query and reorders the list. That is what fixes "how to give stitches":
the query is semantically closer to the Suture article than to sewing
articles, even though the words match sewing better.

FAIL-OPEN by design — this module must never break search:
- If onnxruntime / tokenizers / numpy are missing, rerank() returns
  the list unchanged.
- If the model files are missing or fail to load, rerank() returns
  the list unchanged.
- If a single re-rank takes longer than RERANK_BUDGET_SECONDS, the
  original order is returned.
- The model is loaded lazily on first use (not at hub startup), so a
  slow first load never delays the hub starting.
- A lock serialises inference, so concurrent searches share one
  session safely.

The model lives in models/all-MiniLM-L6-v2/ next to this file
(model.onnx = quantized ONNX, tokenizer.json). The hub itself stays
stdlib-only; this is the one optional dependency.
"""

import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "models", "all-MiniLM-L6-v2")
MODEL_PATH = os.path.join(MODEL_DIR, "model.onnx")
TOKENIZER_PATH = os.path.join(MODEL_DIR, "tokenizer.json")

# Wall-clock budget for one re-rank (encode + inference + scoring).
# MiniLM on the i5-8350U should take well under a second for ~8
# candidates; over the budget, the original order is returned.
RERANK_BUDGET_SECONDS = 3.0
# Longest text fed to the model (title + snippet). MiniLM's max
# sequence length is 256 tokens; truncation keeps it cheap.
MAX_CHARS = 512

_lock = threading.Lock()
_session = None       # onnxruntime InferenceSession, or False if unavailable
_tokenizer = None     # tokenizers Tokenizer, or False if unavailable
_checked = False      # True once we have tried (and failed) to load


def _load():
    """Load the tokenizer + ONNX session once; return (session, tokenizer)
    or (None, None). Never raises — any failure means 'no re-ranker'."""
    global _session, _tokenizer, _checked
    if _checked:
        return (_session or None), (_tokenizer or None)
    _checked = True
    _session = False
    _tokenizer = False
    try:
        if not (os.path.isfile(MODEL_PATH) and os.path.isfile(TOKENIZER_PATH)):
            return None, None
        import numpy  # noqa: F401 - needed by onnxruntime for tensors
        import onnxruntime as ort
        from tokenizers import Tokenizer
        _tokenizer = Tokenizer.from_file(TOKENIZER_PATH)
        _tokenizer.enable_truncation(max_length=256)
        _tokenizer.enable_padding(length=256)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        _session = ort.InferenceSession(MODEL_PATH, opts, providers=["CPUExecutionProvider"])
        return _session, _tokenizer
    except Exception:
        _session = False
        _tokenizer = False
        return None, None


def _embed(session, tokenizer, texts):
    """Mean-pooled, L2-normalised MiniLM embeddings for a list of texts.

    Returns a numpy array of shape (len(texts), 384). The ONNX model
    outputs last_hidden_state; mean pooling over the non-padding tokens
    (attention mask) is the standard sentence-transformers pooling.
    """
    import numpy as np
    enc = [tokenizer.encode(t) for t in texts]
    ids = np.array([e.ids for e in enc], dtype=np.int64)
    mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    # Only pass token_type_ids if this model expects it.
    input_names = {i.name for i in session.get_inputs()}
    if "token_type_ids" in input_names:
        feed["token_type_ids"] = np.zeros_like(ids)
    out = session.run(None, feed)[0]
    mask_f = mask.astype(np.float32)[..., None]
    summed = (out * mask_f).sum(axis=1)
    counts = mask_f.sum(axis=1)
    counts = np.maximum(counts, 1.0)
    mean = summed / counts
    norms = np.linalg.norm(mean, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return mean / norms


def rerank(query, results):
    """Reorder result dicts by MiniLM similarity to the query.

    Each result gets a "_rerank" key (cosine similarity, -1..1) and the
    list is returned sorted by it, highest first. On ANY failure (no
    model, no libraries, timeout, bad input) the list is returned
    unchanged and in its original order. `results` is a list of dicts
    with "title" and "snippet" keys.
    """
    if not results or len(results) < 2:
        return results
    started = time.monotonic()
    with _lock:
        session, tokenizer = _load()
        if session is None or tokenizer is None:
            return results
        try:
            # Title only: the snippet Kiwix search returns is page
            # boilerplate (footer/category text), not a description of
            # the article, and it misleads the embedding — with
            # snippets attached, "How to Fix a Ripped Seam" outscored
            # "Surgical suture" for "how to give stitches". The title
            # is the clean signal of what an article is about.
            texts = [query] + [r.get("title", "") for r in results]
            embs = _embed(session, tokenizer, texts)
            q = embs[0]
            sims = embs[1:] @ q
        except Exception:
            return results
    if time.monotonic() - started > RERANK_BUDGET_SECONDS:
        return results
    scored = sorted(
        zip([float(s) for s in sims], range(len(results)), results),
        key=lambda t: (-t[0], t[1]),
    )
    out = []
    for score, _, res in scored:
        res["_rerank"] = score
        out.append(res)
    return out


def available():
    """True if the re-ranker loaded successfully (model + libraries)."""
    session, tokenizer = _load()
    return session is not None and tokenizer is not None
