"""Pre-download the open embedding and reranker models.

Run once before the first evaluation. Both are small enough for CPU use and
are the models the reported RAG numbers are measured with.
"""
import sys


def main() -> int:
    from sentence_transformers import CrossEncoder, SentenceTransformer

    for name, loader in (
        ("BAAI/bge-small-en-v1.5", SentenceTransformer),
        ("BAAI/bge-reranker-base", CrossEncoder),
    ):
        print(f"downloading {name} ...", flush=True)
        loader(name)
        print(f"  ok: {name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
