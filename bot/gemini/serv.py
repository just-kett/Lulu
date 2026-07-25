from google import genai
from google.genai import types
from dotenv import load_dotenv
import os

load_dotenv()
llm_token = os.getenv("LLM")

client = genai.Client(api_key=llm_token)

def ask_llm(prompt: str) -> str:
    output = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction="Bạn là Remi một cô gái hơi ngốc nghếch nhưng dễ thương, tốt bụng, luôn sẵn sàng giúp đỡ mọi người." \
            "Bạn PHẢI trả lời các câu hỏi theo NGÔN NGỮ của người hỏi một cách thật tự nhiên và KHÔNG QUÁ 50 từ!"
        )
    )
    return output.text
