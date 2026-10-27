import os
import logging
import sqlite3
import time
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

        # Additional AutoMind AI modules. CREATE IF NOT EXISTS preserves existing data.
        conn.execute("""CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL, roll_no TEXT, institution TEXT,
            class_name TEXT, guardian TEXT, phone TEXT, academic_year TEXT,
            created TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL, role TEXT, institution TEXT,
            phone TEXT, joined TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL, attendance_date TEXT NOT NULL,
            status TEXT NOT NULL, notes TEXT,
            UNIQUE(student_id, attendance_date),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS fee_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL, amount REAL NOT NULL,
            fee_month TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Unpaid',
            paid_date TEXT, notes TEXT,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS business_employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL, department TEXT, job_title TEXT,
            phone TEXT, email TEXT, status TEXT DEFAULT 'Active',
            created TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS business_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_type TEXT NOT NULL, title TEXT NOT NULL,
            amount REAL DEFAULT 0, record_date TEXT,
            person TEXT, notes TEXT, created TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory TEXT NOT NULL, created TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL, message TEXT NOT NULL, created TEXT
        )""")


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
Help with office administration, school and college management, business management,
personal task planning, professional writing, project coordination, IT support,
presentations and spreadsheets. Respond in the user's language, including Urdu or
Roman Urdu. Use clear headings and practical steps. Never invent facts or claim you
performed external actions. Use placeholders for missing details.

User request:
{prompt}
"""

    try:
        client = make_client(key)
        if client is None:
            return "Gemini client initialize nahi hua. API key configuration check karein."

        # Retry one time only for likely temporary connection/service/quota errors.
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
                temporary_terms = (
                    "429", "500", "502", "503", "504", "timeout", "timed out",
                    "temporarily unavailable", "connection reset", "connection error",
                    "service unavailable", "resource_exhausted", "unavailable",
                    "internal server error", "quota",
                )
                is_temporary = any(term in error_text for term in temporary_terms)
                if is_temporary and attempt == 0:
                    logger.warning(
                        "Temporary Gemini failure; retrying once. Model=%s, error=%s",
                        MODEL_NAME, str(exc)[:300],
                    )
                    time.sleep(2)
                    continue

                logger.exception("Gemini request failed. Model=%s", MODEL_NAME)
                if "429" in error_text or "resource_exhausted" in error_text or "quota" in error_text:
                    return (
                        "Gemini API ki rate limit ya quota ka masla ho sakta hai. "
                        "Kuch dair baad try karein aur Google AI Studio mein API quota check karein."
                    )
                if any(term in error_text for term in (
                    "401", "403", "permission_denied", "api key not valid", "invalid api key"
                )):
                    return (
                        "Gemini API key ya permission ka masla hai. Streamlit Cloud > "
                        "Manage app > Settings > Secrets mein GEMINI_API_KEY check karein. "
                        "API key kisi ke saath share na karein."
                    )
                if "404" in error_text or "not_found" in error_text or "model not found" in error_text:
                    return (
                        f"Gemini model '{MODEL_NAME}' available nahi lag raha. "
                        "Google AI Studio mein model name verify karein."
                    )
                if is_temporary:
                    return "Gemini service temporary masla kar rahi hai. Thori dair baad dobara try karein."
                return (
                    "Gemini request fail ho gayi. Streamlit Cloud > Manage app > Logs mein "
                    "'Gemini request failed' ki detailed error check karein. API key ya secrets share na karein."
                )
    except Exception:
        logger.exception("AutoMind AI client initialization failed")
        return "AutoMind AI initialize nahi ho saka. API key, requirements.txt aur Streamlit Cloud logs check karein."


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
        "Excel & Data Tools", "Workflow Manager", "Project Planner", "IT Support",
        "School & College Management", "Office & Business Management", "Personal AI Agent"
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




elif page == "School & College Management":
    st.header("🏫 School & College Management")
    st.caption("Basic student, staff, attendance and fee records. Keep backups and verify records before official use.")
    school_tab = st.tabs(["Students", "School Staff", "Attendance", "Fees"])
    with school_tab[0]:
        st.subheader("Student Admissions & Records")
        with st.form("student_admission_form", clear_on_submit=True):
            s_name = st.text_input("Student full name *")
            s_roll = st.text_input("Roll number")
            s_institution = st.text_input("School / College name")
            s_class = st.text_input("Class / Grade")
            s_guardian = st.text_input("Parent / Guardian")
            s_phone = st.text_input("Contact number")
            s_year = st.text_input("Academic year", value=str(date.today().year))
            s_submit = st.form_submit_button("Save Student")
            if s_submit:
                if not s_name.strip():
                    st.warning("Student ka naam zaroori hai.")
                else:
                    try:
                        with connect() as conn:
                            conn.execute("""INSERT INTO students
                                (full_name, roll_no, institution, class_name, guardian, phone, academic_year, created)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (s_name.strip(), s_roll.strip(), s_institution.strip(), s_class.strip(),
                                 s_guardian.strip(), s_phone.strip(), s_year.strip(),
                                 datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                        st.success("Student record save ho gaya.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Student record save nahi ho saka.")
        with connect() as conn:
            students_df = pd.read_sql_query("SELECT * FROM students ORDER BY id DESC", conn)
        st.dataframe(students_df, use_container_width=True, hide_index=True)
        if not students_df.empty:
            st.download_button("Export Students CSV", students_df.to_csv(index=False).encode("utf-8-sig"),
                               "automind_students.csv", "text/csv", key="export_students")
    with school_tab[1]:
        st.subheader("School / College Staff")
        with st.form("school_staff_form", clear_on_submit=True):
            ss_name = st.text_input("Staff full name *")
            ss_role = st.text_input("Role / Designation")
            ss_institution = st.text_input("School / College")
            ss_phone = st.text_input("Phone")
            ss_submit = st.form_submit_button("Add Staff Member")
            if ss_submit:
                if not ss_name.strip():
                    st.warning("Staff ka naam zaroori hai.")
                else:
                    with connect() as conn:
                        conn.execute("INSERT INTO school_staff (full_name, role, institution, phone, joined) VALUES (?, ?, ?, ?, ?)",
                                     (ss_name.strip(), ss_role.strip(), ss_institution.strip(), ss_phone.strip(),
                                      datetime.now().astimezone().strftime("%Y-%m-%d")))
                    st.success("Staff record save ho gaya.")
                    st.rerun()
        with connect() as conn:
            school_staff_df = pd.read_sql_query("SELECT * FROM school_staff ORDER BY id DESC", conn)
        st.dataframe(school_staff_df, use_container_width=True, hide_index=True)
        if not school_staff_df.empty:
            st.download_button("Export School Staff CSV", school_staff_df.to_csv(index=False).encode("utf-8-sig"),
                               "automind_school_staff.csv", "text/csv", key="export_school_staff")
    with school_tab[2]:
        st.subheader("Daily Attendance")
        with connect() as conn:
            attendance_students = pd.read_sql_query("SELECT id, full_name, roll_no, class_name FROM students ORDER BY full_name", conn)
        if attendance_students.empty:
            st.info("Attendance mark karne se pehle Students tab mein student add karein.")
        else:
            student_labels = {f"{r.full_name} | Roll: {r.roll_no or '-'} | Class: {r.class_name or '-'}": int(r.id)
                              for r in attendance_students.itertuples()}
            with st.form("attendance_form", clear_on_submit=True):
                student_label = st.selectbox("Student", list(student_labels.keys()))
                att_date = st.date_input("Attendance date", value=date.today())
                att_status = st.selectbox("Status", ["Present", "Absent", "Late", "Leave"])
                att_notes = st.text_input("Notes (optional)")
                att_submit = st.form_submit_button("Save Attendance")
                if att_submit:
                    try:
                        with connect() as conn:
                            conn.execute("""INSERT INTO attendance (student_id, attendance_date, status, notes)
                                VALUES (?, ?, ?, ?) ON CONFLICT(student_id, attendance_date)
                                DO UPDATE SET status=excluded.status, notes=excluded.notes""",
                                (student_labels[student_label], att_date.isoformat(), att_status, att_notes.strip()))
                        st.success("Attendance save ho gayi.")
                        st.rerun()
                    except Exception:
                        logger.exception("Attendance save failed")
                        st.error("Attendance save nahi ho saki.")
            with connect() as conn:
                att_df = pd.read_sql_query("""SELECT a.id, s.full_name, s.roll_no, s.class_name,
                    a.attendance_date, a.status, a.notes FROM attendance a
                    JOIN students s ON s.id=a.student_id ORDER BY a.attendance_date DESC, s.full_name""", conn)
            st.dataframe(att_df, use_container_width=True, hide_index=True)
            if not att_df.empty:
                st.download_button("Export Attendance CSV", att_df.to_csv(index=False).encode("utf-8-sig"),
                                   "automind_attendance.csv", "text/csv", key="export_attendance")
    with school_tab[3]:
        st.subheader("Fee Records")
        with connect() as conn:
            fee_students = pd.read_sql_query("SELECT id, full_name, roll_no FROM students ORDER BY full_name", conn)
        if fee_students.empty:
            st.info("Fees record karne se pehle student add karein.")
        else:
            fee_labels = {f"{r.full_name} | Roll: {r.roll_no or '-'}": int(r.id) for r in fee_students.itertuples()}
            with st.form("fee_form", clear_on_submit=True):
                fee_student = st.selectbox("Student for fee", list(fee_labels.keys()))
                fee_month = st.text_input("Fee month (e.g. 2026-10)")
                fee_amount = st.number_input("Amount", min_value=0.0, step=500.0)
                fee_status = st.selectbox("Payment status", ["Unpaid", "Paid", "Partially Paid"])
                fee_paid_date = st.date_input("Paid date", value=date.today(), disabled=fee_status != "Paid")
                fee_notes = st.text_input("Fee notes")
                fee_submit = st.form_submit_button("Save Fee Record")
                if fee_submit:
                    if not fee_month.strip():
                        st.warning("Fee month enter karein.")
                    else:
                        with connect() as conn:
                            conn.execute("""INSERT INTO fee_records
                                (student_id, amount, fee_month, status, paid_date, notes)
                                VALUES (?, ?, ?, ?, ?, ?)""",
                                (fee_labels[fee_student], float(fee_amount), fee_month.strip(), fee_status,
                                 fee_paid_date.isoformat() if fee_status == "Paid" else None, fee_notes.strip()))
                        st.success("Fee record save ho gaya.")
                        st.rerun()
            with connect() as conn:
                fees_df = pd.read_sql_query("""SELECT f.id, s.full_name, s.roll_no, f.amount,
                    f.fee_month, f.status, f.paid_date, f.notes FROM fee_records f
                    JOIN students s ON s.id=f.student_id ORDER BY f.id DESC""", conn)
            st.dataframe(fees_df, use_container_width=True, hide_index=True)
            if not fees_df.empty:
                st.download_button("Export Fees CSV", fees_df.to_csv(index=False).encode("utf-8-sig"),
                                   "automind_fees.csv", "text/csv", key="export_fees")


elif page == "Office & Business Management":
    st.header("🏢 Office & Business Management")
    st.caption("Manage employee records, meetings, expenses and business notes.")
    office_tab = st.tabs(["Employees", "Meetings & Records", "Expenses", "Reports"])
    with office_tab[0]:
        st.subheader("Employee Directory")
        with st.form("business_employee_form", clear_on_submit=True):
            emp_name = st.text_input("Employee full name *")
            emp_dept = st.text_input("Department")
            emp_title = st.text_input("Job title")
            emp_phone = st.text_input("Phone")
            emp_email = st.text_input("Email")
            emp_status = st.selectbox("Employment status", ["Active", "On Leave", "Inactive"])
            emp_submit = st.form_submit_button("Save Employee")
            if emp_submit:
                if not emp_name.strip():
                    st.warning("Employee ka naam zaroori hai.")
                else:
                    with connect() as conn:
                        conn.execute("""INSERT INTO business_employees
                            (full_name, department, job_title, phone, email, status, created)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (emp_name.strip(), emp_dept.strip(), emp_title.strip(), emp_phone.strip(),
                             emp_email.strip(), emp_status, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                    st.success("Employee record save ho gaya.")
                    st.rerun()
        with connect() as conn:
            emp_df = pd.read_sql_query("SELECT * FROM business_employees ORDER BY id DESC", conn)
        st.dataframe(emp_df, use_container_width=True, hide_index=True)
        if not emp_df.empty:
            st.download_button("Export Employees CSV", emp_df.to_csv(index=False).encode("utf-8-sig"),
                               "automind_employees.csv", "text/csv", key="export_employees")
    with office_tab[1]:
        st.subheader("Meetings & Business Records")
        with st.form("business_record_form", clear_on_submit=True):
            rec_type = st.selectbox("Record type", ["Meeting", "Office Task", "Project Note", "Other"])
            rec_title = st.text_input("Title / Subject *")
            rec_date = st.date_input("Date", value=date.today())
            rec_person = st.text_input("Responsible person / attendee")
            rec_notes = st.text_area("Details / Minutes / Notes")
            rec_submit = st.form_submit_button("Save Record")
            if rec_submit:
                if not rec_title.strip():
                    st.warning("Title zaroori hai.")
                else:
                    with connect() as conn:
                        conn.execute("""INSERT INTO business_records
                            (record_type, title, record_date, person, notes, created)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (rec_type, rec_title.strip(), rec_date.isoformat(), rec_person.strip(),
                             rec_notes.strip(), datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                    st.success("Record save ho gaya.")
                    st.rerun()
        with connect() as conn:
            rec_df = pd.read_sql_query("SELECT * FROM business_records WHERE record_type != 'Expense' ORDER BY id DESC", conn)
        st.dataframe(rec_df, use_container_width=True, hide_index=True)
        if not rec_df.empty:
            st.download_button("Export Business Records CSV", rec_df.to_csv(index=False).encode("utf-8-sig"),
                               "automind_business_records.csv", "text/csv", key="export_business_records")
    with office_tab[2]:
        st.subheader("Expense Tracker")
        with st.form("business_expense_form", clear_on_submit=True):
            expense_title = st.text_input("Expense title *")
            expense_amount = st.number_input("Amount", min_value=0.0, step=100.0)
            expense_date = st.date_input("Expense date", value=date.today())
            expense_person = st.text_input("Paid by / vendor")
            expense_notes = st.text_input("Notes")
            expense_submit = st.form_submit_button("Save Expense")
            if expense_submit:
                if not expense_title.strip():
                    st.warning("Expense title zaroori hai.")
                else:
                    with connect() as conn:
                        conn.execute("""INSERT INTO business_records
                            (record_type, title, amount, record_date, person, notes, created)
                            VALUES ('Expense', ?, ?, ?, ?, ?, ?)""",
                            (expense_title.strip(), float(expense_amount), expense_date.isoformat(),
                             expense_person.strip(), expense_notes.strip(),
                             datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                    st.success("Expense save ho gaya.")
                    st.rerun()
        with connect() as conn:
            expense_df = pd.read_sql_query("SELECT id, title, amount, record_date, person, notes FROM business_records WHERE record_type='Expense' ORDER BY record_date DESC", conn)
        st.dataframe(expense_df, use_container_width=True, hide_index=True)
        if not expense_df.empty:
            st.metric("Total Recorded Expenses", f"{expense_df['amount'].sum():,.2f}")
            st.download_button("Export Expenses CSV", expense_df.to_csv(index=False).encode("utf-8-sig"),
                               "automind_expenses.csv", "text/csv", key="export_expenses")
    with office_tab[3]:
        with connect() as conn:
            report_emp = pd.read_sql_query("SELECT department, COUNT(*) AS employees FROM business_employees GROUP BY department", conn)
            report_exp = pd.read_sql_query("SELECT substr(record_date,1,7) AS month, SUM(amount) AS total_expenses FROM business_records WHERE record_type='Expense' GROUP BY substr(record_date,1,7) ORDER BY month DESC", conn)
            report_records = pd.read_sql_query("SELECT record_type, COUNT(*) AS records FROM business_records GROUP BY record_type", conn)
        st.subheader("Management Reports")
        st.write("Employees by department")
        st.dataframe(report_emp, use_container_width=True, hide_index=True)
        st.write("Expenses by month")
        st.dataframe(report_exp, use_container_width=True, hide_index=True)
        st.write("Business records by type")
        st.dataframe(report_records, use_container_width=True, hide_index=True)


elif page == "Personal AI Agent":
    st.header("🧑‍💻 Personal AI Agent")
    st.caption("A personal workspace for planning, writing, remembering notes and turning requests into tracked tasks.")
    st.warning("The agent only performs actions implemented in this app. It does not independently access email, files, or external accounts.")
    agent_tab = st.tabs(["Chat with Agent", "Saved Memory", "My Tasks"])
    with agent_tab[0]:
        with connect() as conn:
            history = pd.read_sql_query("SELECT role, message, created FROM agent_messages ORDER BY id DESC LIMIT 30", conn)
        history = history.iloc[::-1] if not history.empty else history
        for row in history.itertuples():
            with st.chat_message("assistant" if row.role == "assistant" else "user"):
                st.markdown(row.message)
        agent_prompt = st.chat_input("Ask your personal agent in Urdu, Roman Urdu or English...")
        if agent_prompt:
            with connect() as conn:
                conn.execute("INSERT INTO agent_messages (role, message, created) VALUES (?, ?, ?)",
                             ("user", agent_prompt, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                memories_df = pd.read_sql_query("SELECT memory FROM agent_memory ORDER BY id DESC LIMIT 15", conn)
                recent_df = pd.read_sql_query("SELECT role, message FROM agent_messages ORDER BY id DESC LIMIT 12", conn)
            memory_text = "\n".join(f"- {m}" for m in memories_df["memory"].tolist()) if not memories_df.empty else "No saved memories."
            context_text = "\n".join(f"{r['role']}: {r['message']}" for _, r in recent_df.iloc[::-1].iterrows())
            with st.spinner("Personal AI Agent is thinking..."):
                agent_answer = ask_ai(
                    "You are the user's personal AI agent inside AutoMind AI. Be practical, transparent, "
                    "and respond in the user's language. Use the saved memory only as context, not as "
                    "verified truth. You can suggest and draft actions but must not claim external actions "
                    "were performed. If asked to create a task, clearly offer a task title and details; "
                    "do not insert a task unless the user uses the task form.\n\n"
                    f"Saved memory:\n{memory_text}\n\nRecent conversation:\n{context_text}\n\n"
                    f"Current request:\n{agent_prompt}",
                    "Personal AI Agent")
            with connect() as conn:
                conn.execute("INSERT INTO agent_messages (role, message, created) VALUES (?, ?, ?)",
                             ("assistant", agent_answer, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
            st.rerun()
    with agent_tab[1]:
        st.subheader("Saved Personal Memory")
        st.caption("Save only non-sensitive preferences or notes. Do not store passwords, API keys or private secrets.")
        with st.form("agent_memory_form", clear_on_submit=True):
            memory_entry = st.text_area("Memory / preference / project note")
            memory_submit = st.form_submit_button("Save Memory")
            if memory_submit:
                if not memory_entry.strip():
                    st.warning("Memory text enter karein.")
                else:
                    with connect() as conn:
                        conn.execute("INSERT INTO agent_memory (memory, created) VALUES (?, ?)",
                                     (memory_entry.strip(), datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                    st.success("Memory saved.")
                    st.rerun()
        with connect() as conn:
            memory_df = pd.read_sql_query("SELECT * FROM agent_memory ORDER BY id DESC", conn)
        st.dataframe(memory_df, use_container_width=True, hide_index=True)
        if not memory_df.empty:
            delete_memory_id = st.number_input("Memory ID to delete", min_value=0, step=1, value=0)
            if st.button("Delete selected memory", key="delete_agent_memory"):
                with connect() as conn:
                    conn.execute("DELETE FROM agent_memory WHERE id=?", (int(delete_memory_id),))
                st.success("If that ID existed, the memory was deleted.")
                st.rerun()
    with agent_tab[2]:
        st.subheader("Create and track a task")
        with st.form("agent_task_form", clear_on_submit=True):
            agent_task_title = st.text_input("Task title")
            agent_task_category = st.selectbox("Category", ["Personal", "Office", "School / College", "Project", "Other"])
            agent_task_priority = st.selectbox("Priority", ["Low", "Medium", "High"])
            agent_task_description = st.text_area("Task details")
            agent_task_submit = st.form_submit_button("Add Task")
            if agent_task_submit:
                if agent_task_title.strip():
                    try:
                        add_task(agent_task_title, agent_task_category, agent_task_priority, agent_task_description)
                        st.success("Task added to Workflow Manager.")
                        st.rerun()
                    except Exception:
                        logger.exception("Agent task creation failed")
                        st.error("Task add nahi ho saka.")
                else:
                    st.warning("Task title enter karein.")
        with connect() as conn:
            agent_tasks = pd.read_sql_query("SELECT * FROM tasks ORDER BY id DESC LIMIT 50", conn)
        st.dataframe(agent_tasks, use_container_width=True, hide_index=True)

st.divider()
st.caption("AutoMind AI | Python • Streamlit • Gemini • Word • PowerPoint • Excel")
