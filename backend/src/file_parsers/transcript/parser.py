from __future__ import annotations

from pathlib import Path

from file_parsers.transcript.enricher import TranscriptEnricher
from file_parsers.transcript.schema import TranscriptDocument
from llm.client import Client


class TranscriptParser:
    def __init__(self, transcript_path: str) -> None:
        self.path = Path(transcript_path)
        if not self.path.exists():
            raise FileNotFoundError(f"Transcript file doesn't exist: {transcript_path}. CWD: {Path.cwd()}")
        self.document: TranscriptDocument | None = None

    def parse(self) -> None:

        # verify the file type
        if self.path.suffix not in (".txt", ".md"):
            raise ValueError("Wrong file type")
        content = self.path.read_text()

        self.document = TranscriptDocument(source_path=self.path, text=content)

    def enrich(self) -> str:

        if self.document is None:
            raise RuntimeError("Call parse() before enrich().")

        client = Client()
        transcript_enricher = TranscriptEnricher(client)
        text = transcript_enricher.enrich(self.document.text)
        self.document.clean_requirements = text
        return text


if __name__ == "__main__":
    import argparse
    import sys

    cli = argparse.ArgumentParser(
        prog="transcript-parser",
        description="Parse a call transcript and produce a structured English summary",
    )

    cli.add_argument("-f", "--filepath", required=True)
    args = cli.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    parser = TranscriptParser(args.filepath)
    parser.parse()
    sys.stdout.write(f"{parser.enrich()}\n")
