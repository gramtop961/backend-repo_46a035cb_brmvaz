import os
import uuid
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db
from datetime import datetime, timezone

# PDF/DOCX text extraction helpers
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

try:
    import docx
except Exception:
    docx = None

from bson import ObjectId


class SignInRequest(BaseModel):
    email: str
    name: Optional[str] = None

class SignInResponse(BaseModel):
    user_id: str
    token: str

class GenerateRequest(BaseModel):
    user_id: str
    job_description: str
    user_material: str

class GeneratedContent(BaseModel):
    title: str
    summary: str
    bullets: List[str]
    cover_letter: str
    header: str
    footer: str
    advice: str

class SaveProfileRequest(BaseModel):
    user_id: str
    content: GeneratedContent
    loom_url: Optional[str] = None
    photo_url: Optional[str] = None

class SaveProfileResponse(BaseModel):
    profile_id: str
    share_slug: str


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Resume Builder API running"}


@app.post("/auth/signin", response_model=SignInResponse)
def signin(payload: SignInRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    # upsert user by email
    user = db["user"].find_one({"email": payload.email})
    if not user:
        user_doc = {
            "email": payload.email,
            "name": payload.name or payload.email.split("@")[0],
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        res = db["user"].insert_one(user_doc)
        user_id = str(res.inserted_id)
    else:
        user_id = str(user["_id"])
        db["user"].update_one({"_id": ObjectId(user_id)}, {"$set": {"name": payload.name or user.get("name"), "updated_at": datetime.now(timezone.utc)}})

    token = str(uuid.uuid4())
    db["session"].insert_one({
        "user_id": ObjectId(user_id),
        "token": token,
        "created_at": datetime.now(timezone.utc)
    })
    return {"user_id": user_id, "token": token}


@app.post("/upload/extract-text")
def extract_text(file: UploadFile = File(...)):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    filename = file.filename or ""
    ext = filename.split(".")[-1].lower()
    content = ""

    try:
        if ext == "pdf":
            if not PdfReader:
                raise HTTPException(status_code=400, detail="PDF support not available")
            import io
            reader = PdfReader(io.BytesIO(file.file.read()))
            texts = []
            for page in reader.pages:
                try:
                    texts.append(page.extract_text() or "")
                except Exception:
                    continue
            content = "\n".join(texts).strip()
        elif ext in ("docx", "doc"):
            if not docx:
                raise HTTPException(status_code=400, detail="DOCX support not available")
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
                tmp.write(file.file.read())
                tmp.flush()
                document = docx.Document(tmp.name)
            paragraphs = [p.text for p in document.paragraphs]
            content = "\n".join(paragraphs).strip()
        else:
            # treat as text
            content = file.file.read().decode("utf-8", errors="ignore")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {str(e)[:120]}")

    return {"text": content}


def simple_ai_generate(job_description: str, user_material: str) -> GeneratedContent:
    # Heuristic generation without external AI dependencies
    jd_lines = [l.strip() for l in job_description.splitlines() if l.strip()]
    um_lines = [l.strip() for l in user_material.splitlines() if l.strip()]

    # Extract likely title
    title = " ".join(jd_lines[0].split()[:6]) if jd_lines else "Professional Profile"

    # Build summary focusing on overlap keywords (simple heuristic)
    def keywords(text: str) -> List[str]:
        import re
        words = re.findall(r"[A-Za-z]{4,}", text.lower())
        common = {}
        for w in words:
            common[w] = common.get(w, 0) + 1
        # top 10
        return [w for w, _ in sorted(common.items(), key=lambda x: x[1], reverse=True)[:10]]

    jd_kw = set(keywords(job_description))
    um_kw = set(keywords(user_material))
    overlap = [w for w in jd_kw.intersection(um_kw)]

    summary = (
        f"Results-driven professional aligning closely with the role's priorities: {', '.join(overlap[:6])}. "
        f"Brings proven experience highlighted below and tailored precisely to the job description."
    )

    # Bullets crafted from user material lines containing keywords
    bullets: List[str] = []
    for line in um_lines[:12]:
        for k in list(jd_kw)[:8]:
            if k in line.lower() and len(bullets) < 8:
                bullets.append(f"Delivered {k}-focused outcomes: {line[:140]}")
                break
    if not bullets:
        bullets = [f"Accomplished key outcomes across {', '.join(list(um_kw)[:5])}."]

    cover_letter = (
        "Dear Hiring Manager,\n\n"
        "I'm excited to apply for this opportunity. After reviewing the job description, I curated the attached resume to emphasize the most relevant "
        "skills and outcomes, including " + ", ".join(list(overlap)[:6]) + ". "
        "I thrive in collaborative, fast-moving environments and would welcome the chance to contribute.\n\n"
        "Sincerely,\nYour Name"
    )

    header = "Impact-forward Resume"
    footer = "Created with Flames Blue Resume Builder"

    advice = (
        "Record a 60–90s Loom: start with a 10s intro (name, role), then 30s on a signature achievement, 20s on how it maps to the JD, "
        "and finish with a clear ask to connect. Smile, good lighting, and share 1 on-screen artifact (dashboard, code snippet, design)."
    )

    return GeneratedContent(
        title=title,
        summary=summary,
        bullets=bullets,
        cover_letter=cover_letter,
        header=header,
        footer=footer,
        advice=advice,
    )


@app.post("/generate", response_model=GeneratedContent)
def generate(payload: GenerateRequest):
    if not payload.job_description.strip() or not payload.user_material.strip():
        raise HTTPException(status_code=400, detail="Both job description and user material are required")
    return simple_ai_generate(payload.job_description, payload.user_material)


@app.post("/profile", response_model=SaveProfileResponse)
def save_profile(payload: SaveProfileRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    try:
        user_oid = ObjectId(payload.user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    share_slug = uuid.uuid4().hex[:10]

    doc = {
        "user_id": user_oid,
        "content": payload.content.model_dump(),
        "loom_url": payload.loom_url,
        "photo_url": payload.photo_url,
        "share_slug": share_slug,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    res = db["profile"].insert_one(doc)
    return {"profile_id": str(res.inserted_id), "share_slug": share_slug}


@app.get("/profile/{slug}")
def get_profile(slug: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    doc = db["profile"].find_one({"share_slug": slug})
    if not doc:
        raise HTTPException(status_code=404, detail="Profile not found")

    # transform ObjectId
    doc["_id"] = str(doc["_id"])
    doc["user_id"] = str(doc["user_id"]) if isinstance(doc["user_id"], ObjectId) else doc["user_id"]
    return doc


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
