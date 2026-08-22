"""Low-budget live smoke check for the configured Spider_XHS backend."""

from __future__ import annotations

import argparse
import asyncio

from jobagent.config import Settings
from jobagent.scraper.xhs_backend import SpiderXhsBackend


async def smoke(query: str, timeout: int) -> None:
    backend = SpiderXhsBackend(Settings())
    print("stage=start", flush=True)
    await asyncio.wait_for(backend.start(), timeout)
    try:
        print("stage=search", flush=True)
        references = await asyncio.wait_for(
            backend.search_notes(query, limit=1, sort=1),
            timeout,
        )
        print({"stage": "search_done", "count": len(references)}, flush=True)
        if not references:
            return
        print("stage=fetch", flush=True)
        note = await asyncio.wait_for(backend.fetch_note(references[0].url), timeout)
        print(
            {
                "stage": "fetch_done",
                "note_id": note.note_id,
                "images": len(note.image_urls),
                "body_chars": len(note.body),
                "published_at": note.published_at,
            },
            flush=True,
        )
    finally:
        await backend.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--timeout", type=int, default=45)
    arguments = parser.parse_args()
    asyncio.run(smoke(arguments.query, arguments.timeout))


if __name__ == "__main__":
    main()
