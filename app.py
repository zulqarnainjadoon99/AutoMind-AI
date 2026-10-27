import os
import time
import logging
import sqlite3
from datetime import datetime, date
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd
import streamlit as st
from google import genai

st.set_page_config(page_title="AutoMind AI", page_icon="🤖", layout="wide")

APP_DIR = Path(__file__).resolve().parent
DB = Path(os.environ.get("AUTOMIND_DB_PATH", str(APP_DIR / "automind.db")))
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
MAX_UPLOAD_MB = 20
MAX_AI_SAMPLE_ROWS = 100

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("automind")


# ---------- DATABASE ----------

def connect():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT,
                priority TEXT,
                description TEXT,
                status TEXT DEFAULT 'Pending',
                created TEXT
            )
        """)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        # Add missing columns only; do not delete or reset existing records.
        for name, kind in [
            ("category", "TEXT"), ("priority", "TEXT"),
            ("description", "TEXT"), ("status", "TEXT DEFAULT 'Pending'"),
            ("created", "TEXT")
        ]:
            if name not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")


def get_tasks():
    with connect() as conn:
        return pd.read_sql_query("SELECT * FROM tasks ORDER BY id DESC", conn)


def add_task(title, category, priority, description):
    title = str(title or "").strip()
    if not title:
        raise ValueError("Task name cannot be empty.")
    if priority not in {"Low", "Medium", "High"}:
        raise ValueError("Invalid priority.")
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks (title, category, priority, description, status, created)
               VALUES (?, ?, ?, ?, 'Pending', ?)""",
            (title, category, priority, str(description or "").strip(),
             datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"))
        )


def change_task(task_id, status):
    if status not in {"Pending", "In Progress", "Completed"}:
        raise ValueError("Invalid task status.")
    with connect() as conn:
        cur = conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, int(task_id)))
        if cur.rowcount == 0:
            raise ValueError("Task not found.")


def remove_task(task_id):
    with connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (int(task_id),))


try:
    init_db()
except Exception:
    logger.exception("Database initialization failed")
    st.error("Database initialize nahi ho saka. Database path aur permissions check karein.")
    st.stop()


# ---------- GEMINI AI ----------

def get_api_key():
    try:
        secret = st.secrets.get("GEMINI_API_KEY", "")
        if secret:
            return str(secret).strip()
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "").strip()


@st.cache_resource
def make_client(key):
    return genai.Client(api_key=key) if key else None


def ask_ai(prompt, mode="Office Assistant"):
    prompt = str(prompt or "").strip()
    if not prompt:
        return "Please enter a request first."

    key = get_api_key()
    if not key:
        return (
            "Gemini API key configure nahi hai. Streamlit Secrets mein "
            '`GEMINI_API_KEY = "your-private-api-key"` add karein, ya environment '
            "variable set karein. Asli API key GitHub par upload na karein."
        )

    instructions = f"""
You are AutoMind AI, a professional office productivity assistant.
Current mode: {mode}.
Help with office administration, professional writing, project coordination,
IT support, presentations, spreadsheets and planning. Respond in the user's
language, including Urdu or Roman Urdu. Use clear headings and practical steps.
Never invent facts or claim you performed external actions. Use placeholders for
missing details.

User request:
{prompt}
"""

    try:
        client = make_client(key)
        if client is None:
            return "Gemini client initialize nahi hua. API key configuration check karein."

        # Try once more if the first request fails due to a temporary API/network issue.
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=MODEL_NAME,
                    contents=instructions,
                )
                answer = getattr(response, "text", None)
                if answer and answer.strip():
                    return answer.strip()

                logger.warning("Gemini returned an empty response. Model=%s", MODEL_NAME)
                return "AI se khaali jawab mila. Apna sawal dobara try karein."

            except Exception as exc:
                error_text = str(exc).lower()
                error_code = getattr(exc, "code", None)
                temporary_terms = (
                    "429", "500", "502", "503", "504", "timeout", "timed out",
                    "temporarily unavailable", "connection reset", "connection error",
                    "service unavailable", "resource_exhausted", "unavailable",
                    "internal server error",
                )
                is_temporary = any(term in error_text for term in temporary_terms)

                if is_temporary and attempt == 0:
                    logger.warning(
                        "Temporary Gemini failure; retrying once. Model=%s, code=%s, error=%s",
                        MODEL_NAME, error_code, str(exc)[:300],
                    )
                    time.sleep(2)
                    continue

                logger.exception(
                    "Gemini request failed. Model=%s, code=%s", MODEL_NAME, error_code
                )

                if ("429" in error_text or "resource_exhausted" in error_text
                        or "quota" in error_text):
                    return (
                        "Gemini API ki rate limit ya quota ka masla ho sakta hai. "
                        "Kuch dair baad try karein aur Google AI Studio mein API quota check karein."
                    )

                if ("401" in error_text or "403" in error_text
                        or "permission_denied" in error_text
                        or "api key not valid" in error_text
                        or "invalid api key" in error_text):
                    return (
                        "Gemini API key ya permission ka masla hai. Streamlit Cloud > "
                        "Manage app > Settings > Secrets mein GEMINI_API_KEY check karein. "
                        "API key kisi ke saath share na karein."
                    )

                if ("404" in error_text or "not_found" in error_text
                        or "model not found" in error_text):
                    return (
                        f"Gemini model '{MODEL_NAME}' available nahi lag raha. "
                        "Google AI Studio mein model name verify karein aur Streamlit Secrets check karein."
                    )

                if is_temporary:
                    return (
                        "Gemini service abhi temporary masla kar rahi hai. Thori dair baad "
                        "dobara try karein. Masla rahe to Streamlit Cloud ke Logs check karein."
                    )

                return (
                    "Gemini request fail ho gayi. Streamlit Cloud > Manage app > Logs mein "
                    "'Gemini request failed' ki detailed error check karein. Error share karte "
                    "waqt API key ya secret information share na karein."
                )

    except Exception:
        logger.exception("AutoMind AI client initialization failed")
        return (
            "AutoMind AI initialize nahi ho saka. Gemini API key, requirements.txt aur "
            "Streamlit Cloud ke Logs check karein. API key share na karein."
        )


# ---------- EXPORT HELPERS ----------

def create_word(title, content):
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("Word export ke liye python-docx install karein.") from exc
    doc = Document()
    doc.add_heading(str(title)[:200], 0)
    for line in str(content).splitlines():
        line = line.strip()
        if line.startswith("### "):
            doc.add_heading(line[4:], level=2)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=1)
        elif line.startswith(("- ", "* ")):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line.startswith(("1. ", "1) ")):
            doc.add_paragraph(line[3:], style="List Number")
        elif line:
            doc.add_paragraph(line)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def create_pdf(title, content):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError as exc:
        raise RuntimeError("PDF export ke liye reportlab install karein.") from exc
    out = BytesIO()
    pdf = SimpleDocTemplate(out, pagesize=A4, rightMargin=45, leftMargin=45,
                            topMargin=45, bottomMargin=45)
    styles = getSampleStyleSheet()
    story = [Paragraph(escape(str(title)), styles["Title"]), Spacer(1, 16)]
    for line in str(content).splitlines():
        line = line.strip()
        if not line:
            story.append(Spacer(1, 7))
        elif line.startswith("## "):
            story.append(Paragraph(escape(line[3:]), styles["Heading1"]))
        elif line.startswith("### "):
            story.append(Paragraph(escape(line[4:]), styles["Heading2"]))
        elif line.startswith(("- ", "* ")):
            story.append(Paragraph("&#8226; " + escape(line[2:]), styles["BodyText"]))
        else:
            story.append(Paragraph(escape(line), styles["BodyText"]))
            story.append(Spacer(1, 5))
    pdf.build(story)
    return out.getvalue()


def create_powerpoint(title, content):
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except ImportError as exc:
        raise RuntimeError("PowerPoint export ke liye python-pptx install karein.") from exc
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    first = prs.slides.add_slide(prs.slide_layouts[0])
    first.shapes.title.text = str(title)[:200]
    if len(first.placeholders) > 1:
        first.placeholders[1].text = "Created with AutoMind AI"
    sections, heading, points = [], "Overview", []
    for line in str(content).splitlines():
        line = line.strip()
        if line.startswith("## "):
            if points:
                sections.append((heading, points))
            heading, points = line[3:][:100], []
        elif line.startswith(("- ", "* ")):
            points.append(line[2:])
        elif line:
            points.append(line)
    if points:
        sections.append((heading, points))
    if not sections:
        sections = [("Content", ["No slide content was generated."])]
    for heading, bullets in sections:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = heading[:100]
        frame = slide.placeholders[1].text_frame
        frame.clear()
        for i, bullet in enumerate(bullets[:7]):
            p = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            p.text = str(bullet)[:400]
            p.font.size = Pt(22)
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def safe_download(label, factory, file_name, mime, key):
    try:
        st.download_button(label, data=factory(), file_name=file_name, mime=mime, key=key)
    except Exception as exc:
        logger.exception("Export failed")
        st.error(str(exc) if isinstance(exc, RuntimeError) else
                 "File export nahi ho saki. Dependencies aur deployment logs check karein.")


# ---------- NAVIGATION ----------

st.title("🤖 AutoMind AI")
st.caption("Your AI-Powered Office Productivity Workspace")
with st.sidebar:
    st.subheader("Workspace")
    page = st.radio("Choose a Tool", [
        "Dashboard", "AI Assistant", "Word & PDF Documents", "PowerPoint Maker",
        "Excel & Data Tools", "Workflow Manager", "Project Planner", "IT Support"
    ])
    st.divider()
    st.caption("Keep API keys in Streamlit Secrets or environment variables.")

tasks = get_tasks()


if page == "Dashboard":
    st.header("📊 Office Dashboard")
    total = len(tasks)
    completed = int((tasks["status"] == "Completed").sum()) if total else 0
    pending = int((tasks["status"] != "Completed").sum()) if total else 0
    progress = int((tasks["status"] == "In Progress").sum()) if total else 0
    a, b, c, d = st.columns(4)
    a.metric("Total Tasks", total)
    b.metric("Completed", completed)
    c.metric("In Progress", progress)
    d.metric("Pending", pending)
    st.progress(completed / total if total else 0,
                text=f"Completion: {completed / total:.0%}" if total else "No tasks yet")
    st.subheader("Recent Tasks")
    if total:
        st.dataframe(tasks.head(20), use_container_width=True, hide_index=True)
    else:
        st.info("Workflow Manager mein pehla task create karein.")
    st.info(
        "SQLite data us server/disk par save hota hai jahan app run hoti hai. Kuch cloud "
        "hosts restart/deploy par local files delete kar sakte hain. Production ke liye "
        "persistent disk ya managed database configure karein."
    )


elif page == "AI Assistant":
    st.header("🧠 Gemini AI Assistant")
    mode = st.selectbox("Work Type", [
        "Office Assistant", "Professional Writing", "Project Management",
        "IT Support", "Presentation Content", "Excel Help", "Daily Planner"
    ])
    prompt = st.text_area("Aap kya kaam karwana chahte hain?", height=180,
                          placeholder="Misal: Create a professional office meeting report...")
    if st.button("Generate with AI", type="primary", key="generate_ai"):
        if prompt.strip():
            with st.spinner("AutoMind AI is working..."):
                st.session_state["ai_answer"] = ask_ai(prompt, mode)
        else:
            st.warning("Pehle apni request enter karein.")
    if st.session_state.get("ai_answer"):
        st.markdown(st.session_state["ai_answer"])
        st.download_button("Download Answer as TXT", st.session_state["ai_answer"],
                           "automind_ai_answer.txt", "text/plain", key="download_ai_answer")


elif page == "Word & PDF Documents":
    st.header("📝 Word & PDF Document Maker")
    doc_type = st.selectbox("Document Type", [
        "Official Letter", "Job Application", "Professional Email", "Office Report",
        "Meeting Minutes", "Meeting Agenda", "Office Memo", "Notice", "CV Content", "Other"
    ])
    language = st.selectbox("Language", ["English", "Urdu", "Roman Urdu", "Bilingual"])
    details = st.text_area("Purpose, names, dates and other details", height=180)
    if st.button("Create Document", type="primary", key="create_document"):
        if details.strip():
            with st.spinner("Drafting document..."):
                st.session_state["document_answer"] = ask_ai(
                    f"Create a {doc_type} in {language}. Use only provided facts and "
                    f"placeholders for missing information.\n\n{details}",
                    "Professional Writing"
                )
                st.session_state["document_title"] = doc_type
        else:
            st.warning("Document ki details enter karein.")
    if st.session_state.get("document_answer"):
        answer = st.session_state["document_answer"]
        title = st.session_state.get("document_title", "Office Document")
        st.subheader("Document Preview")
        st.markdown(answer)
        safe_download("Download Word (.docx)", lambda: create_word(title, answer),
                      "office_document.docx",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      "word_download")
        safe_download("Download PDF", lambda: create_pdf(title, answer),
                      "office_document.pdf", "application/pdf", "pdf_download")
        st.download_button("Download TXT", answer, "office_document.txt",
                           "text/plain", key="txt_document_download")


elif page == "PowerPoint Maker":
    st.header("📽️ AI PowerPoint Presentation Maker")
    topic = st.text_input("Presentation Topic")
    audience = st.text_input("Audience (e.g. students, staff, management)")
    slide_count = st.slider("Number of content slides", 3, 15, 6)
    language = st.selectbox("Presentation Language", ["English", "Urdu", "Roman Urdu"])
    extra = st.text_area("Extra instructions (optional)")
    if st.button("Generate Presentation", type="primary", key="generate_ppt"):
        if topic.strip():
            prompt = f"""
Create PowerPoint content about: {topic}
Audience: {audience or 'General audience'}
Language: {language}
Create approximately {slide_count} content slides.
Start with an introduction. Use a line beginning with ## for each slide heading.
Under each heading write 3 to 5 concise bullet points beginning with - .
Include a conclusion and practical takeaways. Do not use a ## heading for title slide.
Extra instructions: {extra}
"""
            with st.spinner("Generating slide content..."):
                st.session_state["ppt_answer"] = ask_ai(prompt, "Presentation Content")
            st.session_state["ppt_title"] = topic
        else:
            st.warning("Presentation topic enter karein.")
    if st.session_state.get("ppt_answer"):
        answer = st.session_state["ppt_answer"]
        title = st.session_state.get("ppt_title", "Presentation")
        st.subheader("Presentation Content Preview")
        st.markdown(answer)
        safe_download("Download PowerPoint (.pptx)", lambda: create_powerpoint(title, answer),
                      "automind_presentation.pptx",
                      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                      "ppt_download")
        st.download_button("Download Slide Content (.txt)", answer,
                           "presentation_content.txt", "text/plain", key="ppt_txt_download")


elif page == "Excel & Data Tools":
    st.header("📈 Excel & CSV Data Tools")
    st.caption("AI analysis sends up to 100 rows of the file to Gemini. Remove confidential or personal data first.")
    uploaded = st.file_uploader("Upload Excel or CSV file", type=["xlsx", "csv"])
    if uploaded:
        if uploaded.size > MAX_UPLOAD_MB * 1024 * 1024:
            st.error(f"File {MAX_UPLOAD_MB} MB se bari hai. Chhoti file upload karein.")
        else:
            try:
                if uploaded.name.lower().endswith(".csv"):
                    df = pd.read_csv(uploaded, nrows=100000)
                else:
                    df = pd.read_excel(uploaded, nrows=100000)
                st.write(f"Rows loaded: {len(df)} | Columns: {len(df.columns)}")
                st.dataframe(df.head(500), use_container_width=True)
                left, right = st.columns(2)
                left.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                                     "automind_data.csv", "text/csv", key="export_csv")
                excel_buffer = BytesIO()
                with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                    df.to_excel(writer, index=False, sheet_name="Data")
                right.download_button("Download Excel (.xlsx)", excel_buffer.getvalue(),
                                      "automind_data.xlsx",
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                      key="export_xlsx")
                if st.button("Analyze Data with AI", key="analyze_data"):
                    sample = df.head(MAX_AI_SAMPLE_ROWS).to_csv(index=False)
                    with st.spinner("Analyzing sample..."):
                        st.session_state["excel_ai_answer"] = ask_ai(
                            "Analyze this CSV sample: explain columns, visible trends, possible "
                            "data-quality issues and useful next steps. State clearly that only "
                            f"{min(len(df), MAX_AI_SAMPLE_ROWS)} rows were provided.\n\n{sample}",
                            "Excel Help"
                        )
                if st.session_state.get("excel_ai_answer"):
                    st.markdown(st.session_state["excel_ai_answer"])
            except Exception:
                logger.exception("Uploaded file could not be read")
                st.error("File read nahi ho saki. Format, file integrity aur dependencies check karein.")
    st.subheader("Excel Formula Helper")
    goal = st.text_area("What do you want to calculate in Excel?")
    if st.button("Suggest Formula", key="suggest_formula"):
        if goal.strip():
            with st.spinner("Preparing formula..."):
                st.session_state["formula_answer"] = ask_ai(goal, "Excel Help")
        else:
            st.warning("Calculation describe karein.")
    if st.session_state.get("formula_answer"):
        st.markdown(st.session_state["formula_answer"])


elif page == "Workflow Manager":
    st.header("📋 Workflow Manager")
    with st.form("task_form", clear_on_submit=True):
        title = st.text_input("Task / Workflow Name")
        category = st.selectbox("Category", [
            "Project Coordination", "Office Administration", "IT Support",
            "Report Preparation", "General Tasks"
        ])
        priority = st.selectbox("Priority", ["Low", "Medium", "High"])
        description = st.text_area("Description")
        submitted = st.form_submit_button("Create Workflow")
        if submitted:
            if title.strip():
                try:
                    add_task(title, category, priority, description)
                    st.success("Task created!")
                    st.rerun()
                except Exception:
                    logger.exception("Task creation failed")
                    st.error("Task create nahi ho saka. Input aur database status check karein.")
            else:
                st.warning("Task name enter karein.")
    tasks = get_tasks()
    if len(tasks):
        for _, task in tasks.iterrows():
            with st.expander(f"{task['title']} — {task['priority'] or 'No priority'}"):
                st.write("Category:", task["category"] or "Not specified")
                st.write("Description:", task["description"] or "None")
                st.write("Created:", task["created"] or "Unknown")
                statuses = ["Pending", "In Progress", "Completed"]
                current = task["status"] if task["status"] in statuses else "Pending"
                status = st.selectbox("Status", statuses, index=statuses.index(current),
                                      key=f"status_{task['id']}")
                c1, c2 = st.columns(2)
                if c1.button("Save Status", key=f"save_{task['id']}"):
                    try:
                        change_task(task["id"], status)
                        st.rerun()
                    except Exception:
                        logger.exception("Task status update failed")
                        st.error("Status update nahi ho saka.")
                if c2.button("Delete Task", key=f"delete_{task['id']}"):
                    remove_task(task["id"])
                    st.rerun()
        st.download_button("Export Workflows CSV", tasks.to_csv(index=False).encode("utf-8-sig"),
                           "automind_workflows.csv", "text/csv", key="export_workflows")
    else:
        st.info("Abhi koi workflow nahi hai.")


elif page == "Project Planner":
    st.header("🗂️ AI Project Planner")
    name = st.text_input("Project Name")
    goal = st.text_area("Project Goal / Scope")
    deadline = st.date_input("Target Deadline", value=date.today())
    resources = st.text_area("Team, budget or available resources")
    if st.button("Create Project Plan", type="primary", key="create_project_plan"):
        if name.strip() and goal.strip():
            prompt = f"""
Create a practical project plan.
Project: {name}
Goal: {goal}
Target deadline: {deadline}
Resources: {resources or 'Not specified'}
Include summary, phases, milestones, suggested timeline, task owners as placeholders,
risks and mitigations, and a progress report template. Do not invent commitments.
"""
            with st.spinner("Planning project..."):
                st.session_state["project_answer"] = ask_ai(prompt, "Project Management")
        else:
            st.warning("Project name aur goal enter karein.")
    if st.session_state.get("project_answer"):
        answer = st.session_state["project_answer"]
        st.markdown(answer)
        st.download_button("Download Project Plan", answer, "project_plan.txt",
                           "text/plain", key="download_project_plan")


elif page == "IT Support":
    st.header("🛠️ IT Support Assistant")
    issue = st.text_area("Describe your computer, Windows, printer, network or software issue",
                         height=160)
    if st.button("Troubleshoot with AI", type="primary", key="troubleshoot"):
        if issue.strip():
            with st.spinner("Preparing troubleshooting steps..."):
                st.session_state["it_answer"] = ask_ai(issue, "IT Support")
        else:
            st.warning("Issue ki details enter karein.")
    if st.session_state.get("it_answer"):
        answer = st.session_state["it_answer"]
        st.markdown(answer)
        st.download_button("Download Troubleshooting Guide", answer,
                           "it_troubleshooting.txt", "text/plain", key="download_it")


st.divider()
st.caption("AutoMind AI | Python • Streamlit • Gemini • Word • PowerPoint • Excel")
