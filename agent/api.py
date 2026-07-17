import uuid

from fastapi import FastAPI
from phoenix.otel import using_session
from pydantic import BaseModel

from agent.loop import run_agent

app = FastAPI(title="Travel Agent")

CONVERSATIONS: dict[str, list] = {}


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    conversation_id = req.conversation_id or str(uuid.uuid4())
    messages = CONVERSATIONS.get(conversation_id, [])
    messages.append({"role": "user", "content": req.message})
    # Group every span from this turn under the conversation, so evals that need
    # what was established in earlier turns can read the whole thread.
    with using_session(conversation_id):
        reply, messages = run_agent(messages)
    CONVERSATIONS[conversation_id] = messages
    return ChatResponse(reply=reply, conversation_id=conversation_id)
