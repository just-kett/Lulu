from google import genai
from google.genai import types
from dotenv import load_dotenv
import os

load_dotenv()
llm_token = os.getenv("LLM")
system_prompt = os.getenv("SYSTEM_PROMPT")

client = genai.Client(api_key=llm_token)

def _to_contents(history: list[dict]) -> list[types.Content]:
    contents = []
    for item in history:
        role = "user" if item["role"] == "user" else "model"
        contents.append(
            types.Content(role=role, parts=[types.Part(text=item["content"])])
        )
    return contents

def ask_llm(prompt: str, history: list[dict] | None = None) -> str:
    contents = _to_contents(history) if history else prompt
    output = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt
        )
    )
    return output.text
