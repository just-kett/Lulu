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

def ask_llm(
    prompt: str,
    history: list[dict] | None = None,
    extra_instruction: str | None = None,
) -> str:
    contents = _to_contents(history) if history else prompt
    sys_instruction = system_prompt or ""
    if extra_instruction:
        sys_instruction = f"{sys_instruction}\n\n{extra_instruction}".strip()

    output = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=sys_instruction
        )
    )
    return output.text or ""


def condense_memories(existing_notes: str) -> str:
    """Tự động khử trùng lặp, giải quyết mâu thuẫn và cô đọng hồ sơ cá nhân."""
    if not existing_notes or not existing_notes.strip():
        return ""

    instruction = (
        "Bạn là trợ lý quản lý ký ức thông minh. Dưới đây là danh sách các đặc điểm, ghi chú về một thành viên Discord:\n"
        f"{existing_notes}\n\n"
        "Yêu cầu:\n"
        "1. Lọc bỏ các thông tin trùng lặp, tương tự nhau.\n"
        "2. Nếu có thông tin mâu thuẫn (ví dụ vừa thích vừa ghét cùng một thứ), ưu tiên thông tin ở cuối danh sách.\n"
        "3. Tóm tắt cô đọng thành tối đa 4 đến 5 đặc điểm súc tích nhất, ngăn cách nhau bằng dấu gạch đứng ' | '.\n"
        "4. CHỈ TRẢ VỀ chuỗi tóm tắt phân tách bằng ' | ', không thêm bất kỳ lời dẫn giải, mở bài hay định dạng nào khác."
    )
    try:
        output = client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=instruction,
        )
        condensed = (output.text or "").strip()
        if condensed and len(condensed) >= 5:
            return condensed
    except Exception as e:
        print(f"[LuLu Condense Error] Lỗi khi tóm tắt ký ức: {e}")
    return existing_notes
