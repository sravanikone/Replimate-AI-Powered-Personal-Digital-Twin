from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Dict, Any
import uuid
import json
import os

from app.database import get_db, User, ChatSession, ChatHistory, UserMemory, Reminder
from app.rag.loader import load_user_memory
from app.rag.vectorstore import create_vector_store
from app.rag.chain import build_rag_chain
from langchain_google_genai import ChatGoogleGenerativeAI
from dateutil import parser

router = APIRouter()

# --- RAG STATE ---
rag_components: Dict[str, Any] = {}

def reload_rag():
    print("[INFO] Reloading RAG Memory...")
    try:
        docs = load_user_memory()
        vectorstore = create_vector_store(docs)
        chain = build_rag_chain(vectorstore)
        rag_components["chain"] = chain
        print("✅ RAG Ready")
    except Exception as err:
        print("❌ RAG ERROR:", err)


# --- CLEAN FORMAT FUNCTION (🔥 UNIVERSAL FIX) ---
def clean_ai_output(text: str) -> str:
    # Fix brackets
    text = text.replace("( ", "(").replace(" )", ")")
    text = text.replace("\n(", "(").replace(")\n", ")")

    # Fix inline code breaks
    text = text.replace("`\n", "`").replace("\n`", "`")

    # Fix file extensions
    text = text.replace("\n.", ".")

    # Remove excessive empty lines
    lines = text.split("\n")
    cleaned = []

    for line in lines:
        if line.strip():
            cleaned.append(line.rstrip())

    return "\n".join(cleaned)


# --- SCHEMA ---
class ChatRequest(BaseModel):
    user_id: int
    question: str
    session_id: Optional[str] = None


# --- SYSTEM PROMPT (🔥 STRONG VERSION) ---
SYSTEM_PROMPT = """
You are RepliMate, an intelligent AI assistant.

Rules:
- Your name is RepliMate
- Always respond in clean Markdown format

Formatting:
- Use headings (##, ###)
- Use bullet points (-)
- Use proper code blocks (```language)
- Keep everything in a SINGLE COLUMN

Strict Rules:
- NEVER break inline content
- Always keep inline code in one line → (`example`)
- NEVER split brackets:
  ❌ ( example )
  ❌ (
       example
     )
  ✅ (`example`)

- NEVER split file names:
  ❌ HelloWorld
     .java
  ✅ HelloWorld.java

- Avoid unnecessary line breaks
- Keep response clean like ChatGPT
"""


# --- HELPERS ---
def extract_learning(sentence: str):
    triggers = ["i prefer", "i like", "i don't like", "i want", "i hate", "my name is", "i am"]
    for trigger in triggers:
        if trigger in sentence.lower():
            return sentence.strip()
    return None


def process_ai_reminder(user_id: int, question: str, db: Session):
    if not question.lower().startswith(("remind me", "set a reminder", "add reminder", "remind")):
        return None

    llm = ChatGoogleGenerativeAI(
        model="models/gemini-2.5-flash",
        api_key=os.getenv("GOOGLE_API_KEY"),
        temperature=0
    )

    prompt = f"""
Extract reminder JSON from:
"{question}"

Format:
{{
  "content": "...",
  "due_date": "YYYY-MM-DDTHH:MM:SS"
}}
"""

    try:
        response = llm.invoke(prompt)
        data = json.loads(response.content)

        content = data.get("content")
        due_date = parser.parse(data.get("due_date"))

        reminder = Reminder(user_id=user_id, content=content, due_date=due_date)
        db.add(reminder)
        db.commit()

        return f"I've set a reminder: '{content}'."

    except Exception as e:
        print("Reminder error:", e)
        return None


# --- CHAT API ---
@router.post("/chat")
def chat(data: ChatRequest, db: Session = Depends(get_db)):

    # 1️⃣ Session
    session_id = data.session_id
    if not session_id:
        new_sess = ChatSession(
            id=str(uuid.uuid4()),
            user_id=data.user_id,
            title=data.question[:50]
        )
        db.add(new_sess)
        db.commit()
        session_id = new_sess.id

    # 2️⃣ Memory
    memory = extract_learning(data.question)
    if memory:
        db.add(UserMemory(user_id=data.user_id, content=memory))
        db.commit()
        reload_rag()

    # 3️⃣ Reminder
    reminder_response = process_ai_reminder(data.user_id, data.question, db)
    if reminder_response:
        db.add(ChatHistory(user_id=data.user_id, session_id=session_id, role="user", content=data.question))
        db.add(ChatHistory(user_id=data.user_id, session_id=session_id, role="assistant", content=reminder_response))
        db.commit()
        return {"answer": reminder_response, "session_id": session_id}

    # 4️⃣ AI RESPONSE
    try:
        if "chain" not in rag_components:
            reload_rag()

        user = db.query(User).filter(User.id == data.user_id).first()
        name = user.full_name if user and user.full_name else "User"

        full_question = f"""
{SYSTEM_PROMPT}

User name: {name}

Question:
{data.question}
"""

        response = rag_components["chain"].invoke({
            "question": full_question,
            "user_id": data.user_id,
            "user_name": name
        })

        answer = response.content.strip()

        # 🔥 APPLY UNIVERSAL CLEANER
        answer = clean_ai_output(answer)

        # safety
        if "do not have a name" in answer.lower():
            answer = "I am RepliMate, your assistant 😊"

    except Exception as e:
        print("AI ERROR:", e)
        answer = "Error generating response"

    # 5️⃣ Save
    db.add(ChatHistory(user_id=data.user_id, session_id=session_id, role="user", content=data.question))
    db.add(ChatHistory(user_id=data.user_id, session_id=session_id, role="assistant", content=answer))
    db.commit()

    return {"answer": answer, "session_id": session_id}


# --- HISTORY ---
@router.get("/history/{session_id}")
def get_history(session_id: str, db: Session = Depends(get_db)):
    chats = db.query(ChatHistory)\
        .filter(ChatHistory.session_id == session_id)\
        .order_by(ChatHistory.id)\
        .all()

    return [{"role": c.role, "content": c.content} for c in chats]


# --- SESSIONS ---
@router.get("/sessions/{user_id}")
def get_sessions(user_id: int, db: Session = Depends(get_db)):
    sessions = db.query(ChatSession)\
        .filter(ChatSession.user_id == user_id)\
        .order_by(ChatSession.created_at.desc())\
        .all()

    return [{
        "id": s.id,
        "title": s.title,
        "created_at": s.created_at
    } for s in sessions]


# --- DELETE SESSION ---
@router.delete("/session/{session_id}")
def delete_session(session_id: str, db: Session = Depends(get_db)):
    db.query(ChatHistory).filter(ChatHistory.session_id == session_id).delete()
    db.query(ChatSession).filter(ChatSession.id == session_id).delete()
    db.commit()
    return {"message": "Session deleted successfully"}


# --- CLEAR HISTORY ---
@router.delete("/history/{session_id}")
def clear_history(session_id: str, db: Session = Depends(get_db)):
    db.query(ChatHistory).filter(ChatHistory.session_id == session_id).delete()
    db.commit()
    return {"message": "Chat history cleared"}