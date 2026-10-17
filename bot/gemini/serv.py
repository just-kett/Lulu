from google import genai
from google.genai import types
from dotenv import load_dotenv
import os

load_dotenv()
llm_token = os.getenv("LLM")

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
            system_instruction="Bạn là Remi một cô gái hơi ngốc nghếch nhưng dễ thương, tốt bụng, luôn sẵn sàng giúp đỡ mọi người." \
            "Bạn PHẢI trả lời các câu hỏi theo NGÔN NGỮ của người hỏi một cách thật tự nhiên và KHÔNG QUÁ 50 từ!"
        )
    )
    return output.text
