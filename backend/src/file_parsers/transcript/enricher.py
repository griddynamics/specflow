from __future__ import annotations

from pathlib import Path

from llm.client import Client

DEFAULT_MODELS = ["gemini/gemini-3.7-flash", "openai/gpt-5.6-terra", "anthropic/claude-sonnet-5"]
DEFAULT_MODEL = DEFAULT_MODELS[0]
DEFAULT_PROMPT = "format.v2"
MAX_TOKENS = 8000

PROMPTS_DIR = Path(__file__).parent / "prompts"


class TranscriptEnricher:
    def __init__(
        self,
        client: Client,
        model: str = DEFAULT_MODEL,
        prompt_id: str = DEFAULT_PROMPT,
    ) -> None:

        self.client = client
        self.model = model
        self.SYSTEM_PROMPT = (PROMPTS_DIR / f"{prompt_id}.md").read_text()

    def enrich(self, text: str) -> str:

        return self.client.message(
            prompt=text,
            system=self.SYSTEM_PROMPT,
            model=self.model,
            max_tokens=MAX_TOKENS,
        )


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="transcript-enricher",
        description="Summarize a call transcript into structured English Markdown",
    )

    parser.add_argument("-f", "--filepath", required=True)
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL)
    parser.add_argument("-p", "--prompt", default=DEFAULT_PROMPT)
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    filepath = Path(args.filepath)

    transcript = filepath.read_text()

    client = Client()
    transcript_enricher = TranscriptEnricher(client, args.model, args.prompt)
    summary = transcript_enricher.enrich(transcript)

    model_slug = args.model.replace("/", "-")
    output_path = filepath.parent / f"{filepath.stem}.{model_slug}.{args.prompt}.md"
    output_path.write_text(summary)

    sys.stdout.write(f"{summary}\n")
