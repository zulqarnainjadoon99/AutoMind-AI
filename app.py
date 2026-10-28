import os
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

# Responsive polish for narrow screens. Streamlit widgets remain keyboard-accessible.
st.markdown("""
<style>
.block-container { padding-top: 1.15rem; padding-bottom: 2rem; }
[data-testid="stMetric"] { padding: .65rem; border: 1px solid rgba(128,128,128,.22); border-radius: .7rem; }
[data-testid="stDataFrame"] { max-width: 100%; }
button, [data-testid="stDownloadButton"] button { min-height: 2.6rem; }
@media (max-width: 768px) {
  .block-container { padding-left: .75rem; padding-right: .75rem; }
  h1 { font-size: 1.65rem !important; }
  h2 { font-size: 1.35rem !important; }
  h3 { font-size: 1.12rem !important; }
  [data-testid="stHorizontalBlock"] { flex-wrap: wrap; gap: .55rem; }
  [data-testid="column"] { min-width: min(100%, 13rem) !important; flex: 1 1 100% !important; }
  [data-testid="stMetric"] { padding: .5rem; }
  [data-testid="stDataFrame"] { overflow-x: auto; }
  [data-testid="stSidebar"] { min-width: 0; }
}
</style>
""", unsafe_allow_html=True)

APP_DIR = Path(__file__).resolve().parent
DB = Path(os.environ.get("AUTOMIND_DB_PATH", str(APP_DIR / "automind.db")))
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
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

        # School identity/branding used on official-looking printable reports.
        conn.execute("""CREATE TABLE IF NOT EXISTS school_profile (
            id INTEGER PRIMARY KEY CHECK (id=1),
            school_name TEXT NOT NULL,
            address TEXT DEFAULT '', phone TEXT DEFAULT '', email TEXT DEFAULT '',
            logo BLOB NOT NULL, logo_mime TEXT DEFAULT 'image/png', updated TEXT
        )""")
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
            phone TEXT, joined TEXT, teacher_code TEXT, email TEXT,
            qualification TEXT, subjects TEXT, monthly_salary REAL DEFAULT 0,
            employment_status TEXT DEFAULT 'Active', leaving_date TEXT,
            leaving_reason TEXT, replaced_by_id INTEGER,
            FOREIGN KEY(replaced_by_id) REFERENCES school_staff(id)
        )""")
        # Safe schema upgrades: keep all existing staff rows and add missing fields only.
        staff_columns = {r['name'] for r in conn.execute('PRAGMA table_info(school_staff)')}
        for name, kind in [
            ('teacher_code', 'TEXT'), ('email', 'TEXT'), ('qualification', 'TEXT'),
            ('subjects', 'TEXT'), ('monthly_salary', 'REAL DEFAULT 0'),
            ('employment_status', "TEXT DEFAULT 'Active'"), ('leaving_date', 'TEXT'),
            ('leaving_reason', 'TEXT'), ('replaced_by_id', 'INTEGER')
        ]:
            if name not in staff_columns:
                conn.execute(f'ALTER TABLE school_staff ADD COLUMN {name} {kind}')
        conn.execute("""CREATE TABLE IF NOT EXISTS teacher_assignment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            institution TEXT NOT NULL, class_name TEXT, section TEXT, subject TEXT,
            teacher_id INTEGER NOT NULL, start_date TEXT NOT NULL, end_date TEXT,
            notes TEXT, created TEXT,
            FOREIGN KEY(teacher_id) REFERENCES school_staff(id)
        )""")
        # School year/class-specific records. Separate tables keep fees/results archived by year.
        conn.execute("""CREATE TABLE IF NOT EXISTS school_fee_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL, academic_year TEXT NOT NULL,
            class_name TEXT NOT NULL, section TEXT NOT NULL DEFAULT '', roll_no TEXT NOT NULL DEFAULT '',
            fee_month TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
            paid_amount REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Unpaid',
            paid_date TEXT, notes TEXT,
            UNIQUE(student_id, academic_year, fee_month),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        )""")
        # Upgrade older/incompatible fee-ledger schemas additively. Never drop existing records.
        fee_columns = {r["name"] for r in conn.execute("PRAGMA table_info(school_fee_ledger)")}
        fee_additions = [
            ("student_id", "INTEGER"),
            ("academic_year", "TEXT DEFAULT ''"),
            ("class_name", "TEXT DEFAULT ''"),
            ("section", "TEXT DEFAULT ''"),
            ("roll_no", "TEXT DEFAULT ''"),
            ("fee_month", "TEXT DEFAULT ''"),
            ("amount", "REAL DEFAULT 0"),
            ("paid_amount", "REAL DEFAULT 0"),
            ("status", "TEXT DEFAULT 'Unpaid'"),
            ("paid_date", "TEXT"),
            ("notes", "TEXT"),
        ]
        for column_name, column_type in fee_additions:
            if column_name not in fee_columns:
                conn.execute(f"ALTER TABLE school_fee_ledger ADD COLUMN {column_name} {column_type}")
        conn.execute("""CREATE TABLE IF NOT EXISTS school_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL, academic_year TEXT NOT NULL,
            class_name TEXT NOT NULL, section TEXT NOT NULL DEFAULT '', roll_no TEXT NOT NULL DEFAULT '',
            exam_name TEXT NOT NULL, subject TEXT NOT NULL,
            total_marks REAL NOT NULL DEFAULT 100, obtained_marks REAL NOT NULL DEFAULT 0,
            remarks TEXT, created TEXT,
            UNIQUE(student_id, academic_year, exam_name, subject),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        )""")
        # Migrate new columns safely for databases created by earlier app versions.
        student_columns = {r["name"] for r in conn.execute("PRAGMA table_info(students)")}
        if "section" not in student_columns:
            conn.execute("ALTER TABLE students ADD COLUMN section TEXT DEFAULT ''")
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
Help with office administration, professional writing, project coordination,
IT support, presentations, spreadsheets and planning. Respond in the user's
language, including Urdu or Roman Urdu. Use clear headings and practical steps.
Never invent facts or claim you performed external actions. Use placeholders for
missing details.

User request:
{prompt}
"""
    try:
        response = make_client(key).models.generate_content(
            model=MODEL_NAME, contents=instructions
        )
        return getattr(response, "text", None) or "AI se jawab nahi mila. Dobara try karein."
    except Exception:
        logger.exception("Gemini request failed")
        return (
            "Gemini request complete nahi ho saki. API key, internet connection, "
            "quota aur model access check karein. Detailed error server logs mein hai; "
            "API key kisi ke saath share na karein."
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
    st.caption("Class/section-wise student profiles, academic-year archives, monthly fees, attendance and exam results.")
    st.warning("Deployment note: this version uses one shared SQLite database/profile. Do not use it for multiple schools or real sensitive student records until authenticated school-wise data isolation and persistent managed storage are configured.")
    school_tab = st.tabs(["School Profile & Logo", "Student Records", "Attendance", "Monthly Fees", "Results", "Year Archive & Reports", "Teachers & Staff"])

    def get_school_profile():
        with connect() as conn:
            row = conn.execute("SELECT * FROM school_profile WHERE id=1").fetchone()
        return dict(row) if row else None

    def branded_pdf(title, dataframe, profile):
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
        output = BytesIO()
        doc = SimpleDocTemplate(output, pagesize=landscape(A4), rightMargin=12*mm, leftMargin=12*mm, topMargin=10*mm, bottomMargin=12*mm)
        styles = getSampleStyleSheet()
        story = []
        if profile and profile.get("logo"):
            try:
                logo = Image(BytesIO(profile["logo"]), width=22*mm, height=22*mm, kind='proportional')
                logo.hAlign = 'CENTER'
                story.append(logo)
            except Exception:
                pass
        heading = ParagraphStyle('SchoolHeading', parent=styles['Title'], alignment=TA_CENTER, fontSize=16, leading=19, spaceAfter=3)
        subheading = ParagraphStyle('ReportHeading', parent=styles['Heading2'], alignment=TA_CENTER, fontSize=11, leading=14, spaceAfter=8)
        story.append(Paragraph(escape(profile['school_name'] if profile else 'School / College'), heading))
        if profile:
            details = ' | '.join(x for x in [profile.get('address',''), profile.get('phone',''), profile.get('email','')] if x)
            if details: story.append(Paragraph(escape(details), ParagraphStyle('Details', parent=styles['Normal'], alignment=TA_CENTER, fontSize=8)))
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(escape(title), subheading))
        safe_df = dataframe.fillna('').astype(str)
        data = [list(safe_df.columns)] + safe_df.values.tolist()
        if not data[0]: data = [['No data']]
        table = Table(data, repeatRows=1, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#17365D')),('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),7),
            ('GRID',(0,0),(-1,-1),0.35,colors.HexColor('#B7C9D6')),('VALIGN',(0,0),(-1,-1),'TOP'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, colors.HexColor('#F1F5F9')]),
            ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
            ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)
        ]))
        story.append(table)
        doc.build(story)
        return output.getvalue()

    def load_school_students(year=None, class_name=None, section=None):
        query = "SELECT id, full_name, roll_no, institution, class_name, section, guardian, phone, academic_year FROM students WHERE 1=1"
        args = []
        if year and year != "All years": query += " AND academic_year=?"; args.append(year)
        if class_name and class_name != "All classes": query += " AND class_name=?"; args.append(class_name)
        if section and section != "All sections": query += " AND COALESCE(section,'')=?"; args.append(section)
        query += " ORDER BY academic_year DESC, class_name, section, CAST(roll_no AS INTEGER), full_name"
        with connect() as conn:
            return pd.read_sql_query(query, conn, params=args)

    with connect() as conn:
        all_students = pd.read_sql_query("SELECT id, full_name, roll_no, class_name, section, academic_year FROM students ORDER BY academic_year DESC, class_name, section, full_name", conn)
    years = sorted([str(x) for x in all_students["academic_year"].dropna().unique() if str(x).strip()], reverse=True) if not all_students.empty else []
    current_year = str(date.today().year)
    if current_year not in years: years.insert(0, current_year)
    classes = sorted([str(x) for x in all_students["class_name"].dropna().unique() if str(x).strip()]) if not all_students.empty else []
    sections = sorted([str(x) for x in all_students["section"].dropna().unique() if str(x).strip()]) if not all_students.empty else []

    profile = get_school_profile()
    with school_tab[0]:
        st.subheader("School identity and official print branding")
        st.write("School ka naam aur logo set karna zaroori hai. Student lists aur available PDF reports par ye branding print hogi.")
        existing_name = profile.get("school_name", "") if profile else ""
        existing_address = profile.get("address", "") if profile else ""
        existing_phone = profile.get("phone", "") if profile else ""
        existing_email = profile.get("email", "") if profile else ""
        with st.form("school_profile_form_v1"):
            school_name_input = st.text_input("School / College name *", value=existing_name)
            school_address_input = st.text_input("Address", value=existing_address)
            school_phone_input = st.text_input("Phone", value=existing_phone)
            school_email_input = st.text_input("Official email", value=existing_email)
            school_logo_input = st.file_uploader("School logo * (PNG or JPG)", type=["png", "jpg", "jpeg"])
            save_profile = st.form_submit_button("Save School Name & Logo")
            if save_profile:
                logo_bytes = school_logo_input.getvalue() if school_logo_input else (profile.get("logo") if profile else None)
                logo_mime = school_logo_input.type if school_logo_input else (profile.get("logo_mime", "image/png") if profile else "image/png")
                if not school_name_input.strip() or not logo_bytes:
                    st.error("School ka naam aur logo dono lazmi hain. Pehle logo upload karein.")
                else:
                    with connect() as conn:
                        conn.execute("""INSERT INTO school_profile(id,school_name,address,phone,email,logo,logo_mime,updated)
                            VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET school_name=excluded.school_name,
                            address=excluded.address,phone=excluded.phone,email=excluded.email,logo=excluded.logo,
                            logo_mime=excluded.logo_mime,updated=excluded.updated""",
                            (school_name_input.strip(), school_address_input.strip(), school_phone_input.strip(),
                             school_email_input.strip(), sqlite3.Binary(logo_bytes), logo_mime, datetime.now().isoformat(timespec="minutes")))
                    st.success("School name aur logo save ho gaya. PDF reports mein ye details istemal hongi.")
                    st.rerun()
        profile = get_school_profile()
        if profile:
            st.image(profile["logo"], width=110)
            st.markdown(f"### {profile['school_name']}")
            if profile.get("address"): st.caption(profile["address"])
        else:
            st.warning("Pehle school ka naam aur logo save karein. Student records ke PDF print ke liye branding zaroori hai.")

    with school_tab[1]:
        st.subheader("Add student / enrolment")
        st.info("Har academic year ka enrolment alag save karein. Purane saal ke records delete karne ki zaroorat nahi. Roll number se neeche student record search bhi kar sakte hain.")
        with st.form("student_admission_form_v2", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            s_name = c1.text_input("Student full name *")
            s_roll = c2.text_input("Roll number *")
            s_year = c3.text_input("Academic year *", value=current_year)
            c4, c5, c6 = st.columns(3)
            s_institution = c4.text_input("School / College")
            s_class = c5.text_input("Class / Grade *")
            s_section = c6.text_input("Section *", placeholder="A, B, Blue...")
            c7, c8 = st.columns(2)
            s_guardian = c7.text_input("Parent / Guardian")
            s_phone = c8.text_input("Contact number")
            s_submit = st.form_submit_button("Save Student Record")
            if s_submit:
                if not profile:
                    st.error("Pehle School Profile & Logo tab mein school ka naam aur logo save karein.")
                elif not all(x.strip() for x in [s_name, s_roll, s_year, s_class, s_section]):
                    st.warning("Name, roll number, academic year, class aur section lazmi hain.")
                else:
                    with connect() as conn:
                        duplicate = conn.execute("""SELECT id FROM students WHERE roll_no=? AND class_name=? AND COALESCE(section,'')=? AND academic_year=?""", (s_roll.strip(), s_class.strip(), s_section.strip(), s_year.strip())).fetchone()
                        if duplicate:
                            st.error("Is academic year, class, section aur roll number ka record pehle se hai.")
                        else:
                            conn.execute("""INSERT INTO students (full_name, roll_no, institution, class_name, section, guardian, phone, academic_year, created)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (s_name.strip(), s_roll.strip(), s_institution.strip(), s_class.strip(), s_section.strip(), s_guardian.strip(), s_phone.strip(), s_year.strip(), datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                            st.success("Student record save ho gaya.")
                            st.rerun()
        st.subheader("Search student by roll number")
        q1, q2, q3, q4 = st.columns(4)
        search_roll = q1.text_input("Roll number", key="school_search_roll")
        search_year = q2.selectbox("Academic year", ["All years"] + years, key="school_search_year")
        search_class = q3.selectbox("Class", ["All classes"] + classes, key="school_search_class")
        search_section = q4.selectbox("Section", ["All sections"] + sections, key="school_search_section")
        filtered = load_school_students(search_year, search_class, search_section)
        if search_roll.strip(): filtered = filtered[filtered["roll_no"].fillna("").astype(str).str.contains(search_roll.strip(), case=False, regex=False)]
        st.dataframe(filtered, use_container_width=True, hide_index=True)
        if not filtered.empty:
            st.download_button("Export selected student records (CSV)", filtered.to_csv(index=False).encode("utf-8-sig"), "automind_students.csv", "text/csv", key="school_export_students_v2")
            if profile:
                pdf_df = filtered[[c for c in ["roll_no", "full_name", "class_name", "section", "academic_year", "guardian", "phone"] if c in filtered.columns]]
                st.download_button("Download branded student list PDF (school name + logo)", branded_pdf("Student Register", pdf_df, profile), "school_student_register.pdf", "application/pdf", key="school_student_pdf_v1")

    # Shared filters make every module show the chosen class/section/year only.
    with school_tab[2]:
        st.subheader("Class/section-wise daily attendance")
        a1, a2, a3 = st.columns(3)
        att_year = a1.selectbox("Academic year", years, key="att_year_v2")
        att_class = a2.selectbox("Class", classes or ["No classes yet"], key="att_class_v2")
        att_sections = sorted([str(x) for x in all_students.loc[(all_students["academic_year"].astype(str)==att_year) & (all_students["class_name"].astype(str)==att_class), "section"].dropna().unique() if str(x).strip()]) if not all_students.empty and classes else []
        att_section = a3.selectbox("Section", att_sections or ["No sections yet"], key="att_section_v2")
        group = load_school_students(att_year, att_class, att_section)
        if group.empty:
            st.info("Is class/section/year ke students pehle Student Records tab mein add karein.")
        else:
            att_date = st.date_input("Attendance date", value=date.today(), key="att_date_v2")
            with st.form("class_attendance_form_v2"):
                for row in group.itertuples():
                    key = f"att_{row.id}_{att_date.isoformat()}"
                    prior = None
                    with connect() as conn: prior = conn.execute("SELECT status FROM attendance WHERE student_id=? AND attendance_date=?", (int(row.id), att_date.isoformat())).fetchone()
                    default = prior["status"] if prior else "Present"
                    status = st.selectbox(f"Roll {row.roll_no} — {row.full_name}", ["Present", "Absent", "Late", "Leave"], index=["Present", "Absent", "Late", "Leave"].index(default) if default in ["Present", "Absent", "Late", "Leave"] else 0, key=key)
                    st.session_state[f"save_{key}"] = status
                save_att = st.form_submit_button("Save attendance for this class")
                if save_att:
                    with connect() as conn:
                        for row in group.itertuples():
                            key = f"att_{row.id}_{att_date.isoformat()}"
                            status = st.session_state.get(f"save_{key}", "Present")
                            conn.execute("""INSERT INTO attendance(student_id, attendance_date, status, notes) VALUES(?,?,?,?)
                                ON CONFLICT(student_id, attendance_date) DO UPDATE SET status=excluded.status, notes=excluded.notes""", (int(row.id), att_date.isoformat(), status, ""))
                    st.success("Is class/section ki attendance save ho gayi.")
                    st.rerun()
        with connect() as conn:
            att_df = pd.read_sql_query("""SELECT s.academic_year, s.class_name, s.section, s.roll_no, s.full_name, a.attendance_date, a.status FROM attendance a JOIN students s ON s.id=a.student_id ORDER BY a.attendance_date DESC, s.class_name, s.section, s.roll_no""", conn)
        st.dataframe(att_df, use_container_width=True, hide_index=True)
        if not att_df.empty: st.download_button("Export attendance CSV", att_df.to_csv(index=False).encode("utf-8-sig"), "automind_attendance.csv", "text/csv", key="att_export_v2")

    with school_tab[3]:
        st.subheader("Monthly fee ledger")
        st.caption("Har student ke liye har month ka record ek hi dafa save/update hota hai. Purane months aur academic years محفوظ rehte hain.")
        f1, f2, f3 = st.columns(3)
        fee_year = f1.selectbox("Academic year", years, key="fee_year_v2")
        fee_class = f2.selectbox("Class", classes or ["No classes yet"], key="fee_class_v2")
        fee_sec_opts = sorted([str(x) for x in all_students.loc[(all_students["academic_year"].astype(str)==fee_year) & (all_students["class_name"].astype(str)==fee_class), "section"].dropna().unique() if str(x).strip()]) if not all_students.empty and classes else []
        fee_section = f3.selectbox("Section", fee_sec_opts or ["No sections yet"], key="fee_section_v2")
        fee_group = load_school_students(fee_year, fee_class, fee_section)
        if fee_group.empty: st.info("Is class/section/year mein students nahi hain.")
        else:
            fm1, fm2 = st.columns(2)
            fee_month_date = fm1.date_input("Fee month", value=date.today().replace(day=1), key="fee_month_picker_v2")
            fee_month = fee_month_date.strftime("%Y-%m")
            default_fee = fm2.number_input("Monthly fee amount (default)", min_value=0.0, step=500.0, key="default_fee_v2")
            with st.form("monthly_fee_form_v2"):
                fee_values = []
                for row in fee_group.itertuples():
                    with connect() as conn: old = conn.execute("SELECT amount, paid_amount, status, notes FROM school_fee_ledger WHERE student_id=? AND academic_year=? AND fee_month=?", (int(row.id), fee_year, fee_month)).fetchone()
                    st.markdown(f"**Roll {row.roll_no} — {row.full_name}**")
                    x1, x2, x3 = st.columns(3)
                    amount = x1.number_input("Fee due", min_value=0.0, value=float(old["amount"] if old else default_fee), step=500.0, key=f"fee_due_{row.id}_{fee_year}_{fee_month}")
                    paid = x2.number_input("Amount received", min_value=0.0, value=float(old["paid_amount"] if old else 0), step=500.0, key=f"fee_paid_{row.id}_{fee_year}_{fee_month}")
                    notes = x3.text_input("Notes / discount", value=str(old["notes"] or "") if old else "", key=f"fee_notes_{row.id}_{fee_year}_{fee_month}")
                    fee_values.append((row, amount, paid, notes))
                save_fee = st.form_submit_button("Save / Update monthly fee records")
                if save_fee:
                    with connect() as conn:
                        for row, amount, paid, notes in fee_values:
                            status = "Paid" if paid >= amount and amount > 0 else ("Unpaid" if paid <= 0 else "Partially Paid")
                            conn.execute("""INSERT INTO school_fee_ledger(student_id, academic_year, class_name, section, roll_no, fee_month, amount, paid_amount, status, paid_date, notes)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(student_id, academic_year, fee_month) DO UPDATE SET
                                class_name=excluded.class_name, section=excluded.section, roll_no=excluded.roll_no, amount=excluded.amount,
                                paid_amount=excluded.paid_amount, status=excluded.status, paid_date=CASE WHEN excluded.status='Paid' THEN excluded.paid_date ELSE NULL END, notes=excluded.notes""",
                                (int(row.id), fee_year, fee_class, fee_section, str(row.roll_no or ""), fee_month, float(amount), float(paid), status, date.today().isoformat() if status=="Paid" else None, notes.strip()))
                    st.success(f"{fee_month} ki fee entries save/update ho gayi.")
                    st.rerun()
        with connect() as conn:
            fees_df = pd.read_sql_query("""SELECT academic_year, class_name, section, roll_no, s.full_name, fee_month, amount, paid_amount, (amount-paid_amount) AS balance, status, paid_date, notes FROM school_fee_ledger f JOIN students s ON s.id=f.student_id ORDER BY academic_year DESC, fee_month DESC, class_name, section, CAST(roll_no AS INTEGER)""", conn)
        st.dataframe(fees_df, use_container_width=True, hide_index=True)
        if not fees_df.empty:
            st.download_button("Export all fee records CSV", fees_df.to_csv(index=False).encode("utf-8-sig"), "automind_school_fee_ledger.csv", "text/csv", key="fees_export_v2")
            if profile:
                st.download_button("Download branded fee report PDF", branded_pdf("Monthly Fee Ledger", fees_df, profile), "school_fee_ledger.pdf", "application/pdf", key="fees_pdf_v1")
            st.metric("Total balance due", f"{fees_df['balance'].sum():,.2f}")

    with school_tab[4]:
        st.subheader("Exam results / marksheet")
        r1, r2, r3 = st.columns(3)
        res_year = r1.selectbox("Academic year", years, key="res_year_v2")
        res_class = r2.selectbox("Class", classes or ["No classes yet"], key="res_class_v2")
        res_sections = sorted([str(x) for x in all_students.loc[(all_students["academic_year"].astype(str)==res_year) & (all_students["class_name"].astype(str)==res_class), "section"].dropna().unique() if str(x).strip()]) if not all_students.empty and classes else []
        res_section = r3.selectbox("Section", res_sections or ["No sections yet"], key="res_section_v2")
        res_group = load_school_students(res_year, res_class, res_section)
        with st.form("result_entry_form_v2", clear_on_submit=True):
            exam_name = st.text_input("Exam / term name", placeholder="First Term, Midterm, Annual")
            res_subject = st.text_input("Subject")
            res_total = st.number_input("Total marks", min_value=1.0, value=100.0, step=5.0)
            res_roll = st.text_input("Student roll number")
            res_obtained = st.number_input("Obtained marks", min_value=0.0, value=0.0, step=1.0)
            res_remarks = st.text_input("Remarks")
            result_submit = st.form_submit_button("Save / Update subject result")
            if result_submit:
                match = res_group[res_group["roll_no"].fillna("").astype(str)==res_roll.strip()] if not res_group.empty else pd.DataFrame()
                if not exam_name.strip() or not res_subject.strip() or not res_roll.strip(): st.warning("Exam, subject aur roll number enter karein.")
                elif match.empty: st.error("Is class/section/year mein ye roll number nahi mila.")
                elif res_obtained > res_total: st.error("Obtained marks total marks se zyada nahi ho sakte.")
                else:
                    student = match.iloc[0]
                    with connect() as conn:
                        conn.execute("""INSERT INTO school_results(student_id, academic_year, class_name, section, roll_no, exam_name, subject, total_marks, obtained_marks, remarks, created)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(student_id, academic_year, exam_name, subject) DO UPDATE SET
                            class_name=excluded.class_name, section=excluded.section, roll_no=excluded.roll_no, total_marks=excluded.total_marks,
                            obtained_marks=excluded.obtained_marks, remarks=excluded.remarks, created=excluded.created""",
                            (int(student["id"]), res_year, res_class, res_section, res_roll.strip(), exam_name.strip(), res_subject.strip(), float(res_total), float(res_obtained), res_remarks.strip(), datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                    st.success("Result save/update ho gaya.")
                    st.rerun()
        with connect() as conn:
            results_df = pd.read_sql_query("""SELECT r.academic_year, r.class_name, r.section, r.roll_no, s.full_name, r.exam_name, r.subject, r.total_marks, r.obtained_marks, ROUND(100.0*r.obtained_marks/r.total_marks,2) AS percentage, r.remarks FROM school_results r JOIN students s ON s.id=r.student_id ORDER BY r.academic_year DESC, r.exam_name, r.class_name, r.section, CAST(r.roll_no AS INTEGER), r.subject""", conn)
        st.dataframe(results_df, use_container_width=True, hide_index=True)
        if not results_df.empty:
            st.download_button("Export results CSV", results_df.to_csv(index=False).encode("utf-8-sig"), "automind_school_results.csv", "text/csv", key="results_export_v2")
            if profile:
                st.download_button("Download branded results report PDF", branded_pdf("Exam Results", results_df, profile), "school_exam_results.pdf", "application/pdf", key="results_pdf_v1")
            st.info("Ye printable school report hai, official board certificate nahi.")

    with school_tab[5]:
        st.subheader("Academic year archive & reports")
        st.write("Har year ka record year/class/section ke saath save hota hai. Agle saal naye enrolments add karein; purane saal ki fee, attendance aur results history yahan rahegi.")
        archive_year = st.selectbox("Archive academic year", ["All years"] + years, key="archive_year_v2")
        archive_class = st.selectbox("Archive class", ["All classes"] + classes, key="archive_class_v2")
        archive_section = st.selectbox("Archive section", ["All sections"] + sections, key="archive_section_v2")
        archive_students = load_school_students(archive_year, archive_class, archive_section)
        st.metric("Matching enrolments", len(archive_students))
        st.dataframe(archive_students, use_container_width=True, hide_index=True)
        if not archive_students.empty:
            st.download_button("Download year/class archive CSV", archive_students.to_csv(index=False).encode("utf-8-sig"), "automind_school_archive.csv", "text/csv", key="archive_export_v2")
            if profile:
                st.download_button("Download branded archive PDF", branded_pdf("Academic Year Archive", archive_students, profile), "school_academic_archive.pdf", "application/pdf", key="archive_pdf_v1")
        with connect() as conn:
            fee_report = pd.read_sql_query("SELECT academic_year, class_name, section, fee_month, SUM(amount) AS billed, SUM(paid_amount) AS received, SUM(amount-paid_amount) AS balance FROM school_fee_ledger GROUP BY academic_year, class_name, section, fee_month ORDER BY academic_year DESC, fee_month DESC, class_name, section", conn)
            result_report = pd.read_sql_query("SELECT academic_year, class_name, section, exam_name, COUNT(DISTINCT student_id) AS students, ROUND(AVG(100.0*obtained_marks/total_marks),2) AS average_percentage FROM school_results GROUP BY academic_year, class_name, section, exam_name ORDER BY academic_year DESC, class_name, section, exam_name", conn)
        st.subheader("Monthly fee summary by class/section")
        st.dataframe(fee_report, use_container_width=True, hide_index=True)
        st.subheader("Exam summary by class/section")
        st.dataframe(result_report, use_container_width=True, hide_index=True)
        if not fee_report.empty: st.download_button("Download fee summary CSV", fee_report.to_csv(index=False).encode("utf-8-sig"), "automind_fee_summary.csv", "text/csv", key="fee_summary_export_v2")
        if not result_report.empty: st.download_button("Download exam summary CSV", result_report.to_csv(index=False).encode("utf-8-sig"), "automind_exam_summary.csv", "text/csv", key="exam_summary_export_v2")

    with school_tab[6]:
        st.subheader("Teacher & Staff Directory")
        st.caption("Purane teacher ko delete na karein. Status update karein aur replacement ko alag record ke taur par add karein.")
        with st.form("school_staff_form_v3", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                ss_name = st.text_input("Teacher / Staff full name *")
                ss_code = st.text_input("Teacher ID (optional)")
                ss_role = st.text_input("Role / Designation", value="Teacher")
                ss_institution = st.text_input("School / College name *")
                ss_phone = st.text_input("Phone")
                ss_email = st.text_input("Email")
            with c2:
                ss_qualification = st.text_input("Qualification")
                ss_subjects = st.text_input("Subjects / Classes assigned")
                ss_salary = st.number_input("Monthly salary", min_value=0.0, step=1000.0)
                ss_joined = st.date_input("Joining date", value=date.today())
            ss_submit = st.form_submit_button("Save Teacher / Staff Record")
            if ss_submit:
                if not ss_name.strip() or not ss_institution.strip():
                    st.warning("Teacher ka naam aur school/college ka naam zaroori hai.")
                else:
                    with connect() as conn:
                        conn.execute("""INSERT INTO school_staff
                            (full_name, role, institution, phone, joined, teacher_code, email,
                             qualification, subjects, monthly_salary, employment_status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active')""",
                            (ss_name.strip(), ss_role.strip(), ss_institution.strip(), ss_phone.strip(),
                             ss_joined.isoformat(), ss_code.strip(), ss_email.strip(), ss_qualification.strip(),
                             ss_subjects.strip(), ss_salary))
                    st.success("Teacher/staff record save ho gaya.")
                    st.rerun()

        with connect() as conn:
            staff_df = pd.read_sql_query("""SELECT id, teacher_code, full_name, role, institution, phone,
                email, qualification, subjects, monthly_salary, joined, employment_status,
                leaving_date, leaving_reason, replaced_by_id FROM school_staff ORDER BY id DESC""", conn)
        if not staff_df.empty:
            school_names = sorted([x for x in staff_df['institution'].dropna().unique().tolist() if str(x).strip()])
            chosen_school = st.selectbox("Filter by school / college", ["All schools"] + school_names, key="staff_school_filter_v3")
            shown_staff = staff_df if chosen_school == "All schools" else staff_df[staff_df['institution'] == chosen_school]
            st.dataframe(shown_staff, use_container_width=True, hide_index=True)
            st.download_button("Export teacher/staff records CSV", shown_staff.to_csv(index=False).encode("utf-8-sig"), "automind_teacher_staff.csv", "text/csv", key="staff_export_v3")

            st.divider()
            st.subheader("Teacher Leaving & Replacement")
            active_df = staff_df[staff_df['employment_status'].fillna('Active').eq('Active')]
            if active_df.empty:
                st.info("Is waqt koi active staff record nahi mila. Pehle teacher add karein.")
            else:
                with st.form("teacher_leaving_form_v3", clear_on_submit=True):
                    active_options = {f"ID {int(r.id)} — {r.full_name} ({r.institution})": int(r.id) for r in active_df.itertuples()}
                    leaving_label = st.selectbox("Teacher leaving / status update", list(active_options.keys()))
                    leave_status = st.selectbox("New status", ["Resigned / Left", "Transferred", "Retired", "Terminated", "On Leave"])
                    leave_date = st.date_input("Effective date", value=date.today(), key="teacher_leave_date_v3")
                    leave_reason = st.text_area("Reason / notes")
                    replacement_choices = {"No replacement linked yet": None}
                    replacement_df = staff_df[(staff_df['employment_status'].fillna('Active').eq('Active'))]
                    replacement_choices.update({f"ID {int(r.id)} — {r.full_name} ({r.institution})": int(r.id) for r in replacement_df.itertuples()})
                    replacement_label = st.selectbox("Replacement teacher (if already added)", list(replacement_choices.keys()))
                    leave_submit = st.form_submit_button("Save Leaving / Replacement History")
                    if leave_submit:
                        old_id = active_options[leaving_label]
                        new_id = replacement_choices[replacement_label]
                        if new_id == old_id:
                            st.error("Purane teacher ko usi ke replacement ke taur par select nahi kar sakte.")
                        else:
                            with connect() as conn:
                                conn.execute("UPDATE school_staff SET employment_status=?, leaving_date=?, leaving_reason=?, replaced_by_id=? WHERE id=?",
                                    (leave_status, leave_date.isoformat(), leave_reason.strip(), new_id, old_id))
                                # Close any current assignment-history rows for the outgoing teacher.
                                conn.execute("UPDATE teacher_assignment_history SET end_date=? WHERE teacher_id=? AND end_date IS NULL", (leave_date.isoformat(), old_id))
                            st.success("Teacher leaving history save ho gayi. Purana record aur uski history delete nahi hui.")
                            st.rerun()

            st.divider()
            st.subheader("Class / Subject Assignment History")
            all_teacher_options = {f"ID {int(r.id)} — {r.full_name} ({r.institution})": int(r.id) for r in staff_df.itertuples()}
            with st.form("teacher_assignment_form_v3", clear_on_submit=True):
                teacher_label = st.selectbox("Teacher", list(all_teacher_options.keys()), key="assignment_teacher_v3")
                a1, a2, a3 = st.columns(3)
                with a1: assign_class = st.text_input("Class")
                with a2: assign_section = st.text_input("Section")
                with a3: assign_subject = st.text_input("Subject")
                assign_start = st.date_input("Assignment start date", value=date.today())
                assign_notes = st.text_input("Notes")
                assignment_submit = st.form_submit_button("Save Assignment")
                if assignment_submit:
                    with connect() as conn:
                        conn.execute("INSERT INTO teacher_assignment_history (institution, class_name, section, subject, teacher_id, start_date, notes, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (str(staff_df.loc[staff_df['id'] == all_teacher_options[teacher_label], 'institution'].iloc[0]), assign_class.strip(), assign_section.strip(), assign_subject.strip(), all_teacher_options[teacher_label], assign_start.isoformat(), assign_notes.strip(), datetime.now().isoformat(timespec='seconds')))
                    st.success("Assignment history save ho gayi.")
                    st.rerun()
            with connect() as conn:
                assignment_df = pd.read_sql_query("""SELECT h.id, h.institution, s.full_name AS teacher, s.teacher_code,
                    h.class_name, h.section, h.subject, h.start_date, h.end_date, h.notes
                    FROM teacher_assignment_history h JOIN school_staff s ON s.id=h.teacher_id
                    ORDER BY h.start_date DESC, h.id DESC""", conn)
            st.dataframe(assignment_df, use_container_width=True, hide_index=True)
            if not assignment_df.empty:
                st.download_button("Export assignment history CSV", assignment_df.to_csv(index=False).encode("utf-8-sig"), "teacher_assignment_history.csv", "text/csv", key="assignment_export_v3")
        else:
            st.info("Abhi staff record nahi hai. Upar form se pehla teacher add karein.")

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
