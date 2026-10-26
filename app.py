
import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, date
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from google import genai

st.set_page_config(
    page_title="AutoMind AI",
    page_icon="🤖",
    layout="wide"
)

DB = Path(__file__).with_name("automind.db")


# ---------- DATABASE ----------

def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
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


def get_tasks():
    with connect() as conn:
        return pd.read_sql_query(
            "SELECT * FROM tasks ORDER BY id DESC", conn
        )


def add_task(title, category, priority, description):
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (title, category, priority, description, status, created)
            VALUES (?, ?, ?, ?, 'Pending', ?)""",
            (
                title, category, priority, description,
                datetime.now().strftime("%Y-%m-%d %H:%M")
            )
        )


def change_task(task_id, status):
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status=? WHERE id=?",
            (status, int(task_id))
        )


def remove_task(task_id):
    with connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (int(task_id),))


init_db()


# ---------- GEMINI AI ----------

try:
    api_key = st.secrets.get("GEMINI_API_KEY", "")
except Exception:
    api_key = ""

ai_client = genai.Client(api_key=api_key) if api_key else None


def ask_ai(prompt, mode="Office Assistant"):
    if not ai_client:
        return (
            "Gemini API key configured nahi hai. Streamlit app ke "
            "Settings > Secrets mein ye add karein:\n\n"
            'GEMINI_API_KEY = "your-private-api-key"\n\n'
            "Apni asli API key GitHub par share na karein."
        )

    instructions = f"""
You are AutoMind AI, a helpful professional office assistant.
Current mode: {mode}.

Help with office administration, professional writing, project
coordination, IT support, presentations, spreadsheets and planning.
Respond in the language the user uses, including Urdu or Roman Urdu.
Use clear headings and practical steps. Never invent facts or claim
you have performed external actions. Mark unknown details as placeholders.

User request:
{prompt}
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=instructions
        )
        return response.text or "AI se jawab nahi mila. Dobara try karein."
    except Exception as error:
        return (
            "Gemini request failed. API key, quota aur model access "
            "check karein.\n\n" + str(error)[:400]
        )


# ---------- WORD EXPORT ----------

def create_word(title, content):
    from docx import Document

    doc = Document()
    doc.add_heading(title, 0)

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("## "):
            doc.add_heading(line[3:], level=1)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=2)
        elif line.startswith("- "):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line:
            doc.add_paragraph(line)

    output = BytesIO()
    doc.save(output)
    return output.getvalue()


# ---------- PDF EXPORT ----------

def create_pdf(title, content):
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer
    )
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch

    output = BytesIO()
    pdf = SimpleDocTemplate(
        output, pagesize=A4,
        rightMargin=45, leftMargin=45,
        topMargin=45, bottomMargin=45
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(escape(title), styles["Title"]), Spacer(1, 16)]

    for line in content.splitlines():
        line = line.strip()
        if not line:
            story.append(Spacer(1, 7))
        elif line.startswith("## "):
            story.append(
                Paragraph(escape(line[3:]), styles["Heading1"])
            )
        elif line.startswith("### "):
            story.append(
                Paragraph(escape(line[4:]), styles["Heading2"])
            )
        elif line.startswith("- "):
            story.append(
                Paragraph("&#8226; " + escape(line[2:]), styles["BodyText"])
            )
        else:
            story.append(Paragraph(escape(line), styles["BodyText"]))
            story.append(Spacer(1, 5))

    pdf.build(story)
    return output.getvalue()


# ---------- POWERPOINT EXPORT ----------

def create_powerpoint(title, content):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title
    slide.placeholders[1].text = "Created with AutoMind AI"

    sections = []
    heading = "Overview"
    points = []

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("## "):
            if points:
                sections.append((heading, points))
            heading = line[3:]
            points = []
        elif line.startswith(("- ", "* ")):
            points.append(line[2:])
        elif line:
            points.append(line)

    if points:
        sections.append((heading, points))

    for heading, bullets in sections:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = heading[:100]
        frame = slide.placeholders[1].text_frame
        frame.clear()

        for i, bullet in enumerate(bullets[:7]):
            paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            paragraph.text = bullet[:400]
            paragraph.font.size = Pt(22)

    output = BytesIO()
    prs.save(output)
    return output.getvalue()


# ---------- APP NAVIGATION ----------

st.title("🤖 AutoMind AI")
st.caption("Your AI-Powered Office Productivity Workspace")

page = st.sidebar.radio(
    "Choose a Tool",
    [
        "Dashboard",
        "AI Assistant",
        "Word & PDF Documents",
        "PowerPoint Maker",
        "Excel & Data Tools",
        "Workflow Manager",
        "Project Planner",
        "IT Support"
    ]
)

tasks = get_tasks()


# ---------- DASHBOARD ----------

if page == "Dashboard":
    st.header("📊 Office Dashboard")

    total = len(tasks)
    completed = int((tasks["status"] == "Completed").sum()) if total else 0
    pending = total - completed

    a, b, c = st.columns(3)
    a.metric("Total Tasks", total)
    b.metric("Completed", completed)
    c.metric("Pending", pending)

    st.progress(completed / total if total else 0,
                text=f"Completion: {completed / total:.0%}" if total else "No tasks yet")

    st.subheader("Recent Tasks")
    if total:
        st.dataframe(tasks, use_container_width=True, hide_index=True)
    else:
        st.info("Sidebar se pehla task create karein.")

    st.info(
        "Note: Cloud hosting par local database ka data restarts ke "
        "baad persist karna guaranteed nahi. Reliable storage ke liye "
        "persistent ya external database configure karein."
    )


# ---------- AI ASSISTANT ----------

elif page == "AI Assistant":
    st.header("🧠 Gemini AI Assistant")

    mode = st.selectbox(
        "Work Type",
        [
            "Office Assistant", "Professional Writing",
            "Project Management", "IT Support",
            "Presentation Content", "Excel Help", "Daily Planner"
        ]
    )

    prompt = st.text_area(
        "Aap kya kaam karwana chahte hain?",
        height=180,
        placeholder="Misal: Create a professional office meeting report..."
    )

    if st.button("Generate with AI", type="primary"):
        if prompt.strip():
            with st.spinner("AutoMind AI is working..."):
                answer = ask_ai(prompt, mode)
            st.session_state["ai_answer"] = answer

    if st.session_state.get("ai_answer"):
        answer = st.session_state["ai_answer"]
        st.markdown(answer)
        st.download_button(
            "Download Answer as TXT",
            answer,
            file_name="automind_ai_answer.txt",
            mime="text/plain"
        )


# ---------- WORD AND PDF ----------

elif page == "Word & PDF Documents":
    st.header("📝 Word & PDF Document Maker")

    doc_type = st.selectbox(
        "Document Type",
        [
            "Official Letter", "Job Application",
            "Professional Email", "Office Report",
            "Meeting Minutes", "Meeting Agenda",
            "Office Memo", "Notice", "CV Content", "Other"
        ]
    )

    language = st.selectbox(
        "Language", ["English", "Urdu", "Roman Urdu", "Bilingual"]
    )

    details = st.text_area(
        "Purpose, names, dates and other details",
        height=180
    )

    if st.button("Create Document", type="primary"):
        if details.strip():
            with st.spinner("Drafting document..."):
                answer = ask_ai(
                    f"Create a {doc_type} in {language}. "
                    "Use only the provided facts. Use placeholders "
                    "for missing information.\n\n" + details,
                    "Professional Writing"
                )
            st.session_state["document_answer"] = answer
            st.session_state["document_title"] = doc_type

    if st.session_state.get("document_answer"):
        answer = st.session_state["document_answer"]
        title = st.session_state.get("document_title", "Office Document")

        st.subheader("Document Preview")
        st.markdown(answer)

        st.download_button(
            "Download Word (.docx)",
            create_word(title, answer),
            file_name="office_document.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

        st.download_button(
            "Download PDF",
            create_pdf(title, answer),
            file_name="office_document.pdf",
            mime="application/pdf"
        )

        st.download_button(
            "Download TXT",
            answer,
            file_name="office_document.txt",
            mime="text/plain"
        )


# ---------- POWERPOINT MAKER ----------

elif page == "PowerPoint Maker":
    st.header("📽️ AI PowerPoint Presentation Maker")

    topic = st.text_input("Presentation Topic")
    audience = st.text_input("Audience (e.g. students, staff, management)")
    slide_count = st.slider("Number of content slides", 3, 15, 6)
    language = st.selectbox(
        "Presentation Language", ["English", "Urdu", "Roman Urdu"]
    )
    extra = st.text_area("Extra instructions (optional)")

    if st.button("Generate Presentation", type="primary"):
        if topic.strip():
            prompt = f"""
Create PowerPoint content for a presentation about: {topic}
Audience: {audience or 'General audience'}
Language: {language}
Create exactly about {slide_count} content slides.

IMPORTANT FORMAT:
Start with an introduction section.
Use a line beginning with ## for each slide heading.
Under each heading write 3 to 5 concise bullet points beginning with - .
Include a conclusion and practical takeaways.
Add speaker notes in a short separate bullet when useful.
Extra instructions: {extra}
Do not use a ## heading for the title slide.
"""
            with st.spinner("Generating slide content..."):
                answer = ask_ai(prompt, "Presentation Content")
            st.session_state["ppt_answer"] = answer
            st.session_state["ppt_title"] = topic
        else:
            st.warning("Presentation topic enter karein.")

    if st.session_state.get("ppt_answer"):
        answer = st.session_state["ppt_answer"]
        title = st.session_state.get("ppt_title", "Presentation")

        st.subheader("Presentation Content Preview")
        st.markdown(answer)

        st.download_button(
            "Download PowerPoint (.pptx)",
            create_powerpoint(title, answer),
            file_name="automind_presentation.pptx",
            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )

        st.download_button(
            "Download Slide Content (.txt)",
            answer,
            file_name="presentation_content.txt",
            mime="text/plain"
        )


# ---------- EXCEL AND DATA ----------

elif page == "Excel & Data Tools":
    st.header("📈 Excel & CSV Data Tools")

    uploaded = st.file_uploader(
        "Upload Excel or CSV file",
        type=["xlsx", "csv"]
    )

    if uploaded:
        try:
            if uploaded.name.lower().endswith(".csv"):
                df = pd.read_csv(uploaded)
            else:
                df = pd.read_excel(uploaded)

            st.write(f"Rows: {len(df)} | Columns: {len(df.columns)}")
            st.dataframe(df.head(500), use_container_width=True)

            x, y = st.columns(2)

            x.download_button(
                "Download CSV",
                df.to_csv(index=False).encode("utf-8"),
                file_name="automind_data.csv",
                mime="text/csv"
            )

            excel_buffer = BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Data")

            y.download_button(
                "Download Excel (.xlsx)",
                excel_buffer.getvalue(),
                file_name="automind_data.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            if st.button("Analyze Data with AI"):
                sample = df.head(100).to_csv(index=False)
                with st.spinner("Analyzing sample..."):
                    answer = ask_ai(
                        "Analyze this CSV sample. Explain the columns, "
                        "visible trends, possible data quality issues and "
                        "useful next steps. State that only a sample was "
                        "provided if the file has more than 100 rows.\n\n"
                        + sample,
                        "Excel Help"
                    )
                st.markdown(answer)

        except Exception as error:
            st.error(f"File could not be read: {error}")

    st.subheader("Excel Formula Helper")
    goal = st.text_area("What do you want to calculate in Excel?")

    if st.button("Suggest Formula"):
        if goal.strip():
            st.markdown(ask_ai(goal, "Excel Help"))
        else:
            st.warning("Calculation describe karein.")


# ---------- WORKFLOW MANAGER ----------

elif page == "Workflow Manager":
    st.header("📋 Workflow Manager")

    with st.form("task_form", clear_on_submit=True):
        title = st.text_input("Task / Workflow Name")
        category = st.selectbox(
            "Category",
            [
                "Project Coordination", "Office Administration",
                "IT Support", "Report Preparation", "General Tasks"
            ]
        )
        priority = st.selectbox("Priority", ["Low", "Medium", "High"])
        description = st.text_area("Description")
        submitted = st.form_submit_button("Create Workflow")

        if submitted:
            if title.strip():
                add_task(title, category, priority, description)
                st.success("Task created!")
                st.rerun()
            else:
                st.warning("Task name enter karein.")

    tasks = get_tasks()

    if len(tasks):
        for _, task in tasks.iterrows():
            with st.expander(f"{task['title']} — {task['priority']}"):
                st.write("Category:", task["category"])
                st.write("Description:", task["description"] or "None")
                st.write("Created:", task["created"])

                statuses = ["Pending", "In Progress", "Completed"]
                current = task["status"] if task["status"] in statuses else "Pending"

                new_status = st.selectbox(
                    "Status", statuses,
                    index=statuses.index(current),
                    key=f"status_{task['id']}"
                )

                col1, col2 = st.columns(2)

                if col1.button("Save Status", key=f"save_{task['id']}"):
                    change_task(task["id"], new_status)
                    st.rerun()

                if col2.button("Delete Task", key=f"delete_{task['id']}"):
                    remove_task(task["id"])
                    st.rerun()

        st.download_button(
            "Export Workflows CSV",
            tasks.to_csv(index=False).encode("utf-8"),
            file_name="automind_workflows.csv",
            mime="text/csv"
        )
    else:
        st.info("Abhi koi workflow nahi hai.")


# ---------- PROJECT PLANNER ----------

elif page == "Project Planner":
    st.header("🗂️ AI Project Planner")

    name = st.text_input("Project Name")
    goal = st.text_area("Project Goal / Scope")
    deadline = st.date_input("Target Deadline", value=date.today())
    resources = st.text_area("Team, budget or available resources")

    if st.button("Create Project Plan", type="primary"):
        if name.strip() and goal.strip():
            prompt = f"""
Create a practical project plan.
Project: {name}
Goal: {goal}
Target deadline: {deadline}
Resources: {resources or 'Not specified'}

Include project summary, phases, milestones, suggested timeline,
task owners as placeholders, risks and mitigations, and a progress
report template. Do not invent confirmed commitments.
"""
            with st.spinner("Planning project..."):
                answer = ask_ai(prompt, "Project Management")

            st.markdown(answer)
            st.download_button(
                "Download Project Plan",
                answer,
                file_name="project_plan.txt",
                mime="text/plain"
            )
        else:
            st.warning("Project name aur goal enter karein.")


# ---------- IT SUPPORT ----------

elif page == "IT Support":
    st.header("🛠️ IT Support Assistant")

    issue = st.text_area(
        "Describe your computer, Windows, printer, network or software issue",
        height=160
    )

    if st.button("Troubleshoot with AI", type="primary"):
        if issue.strip():
            with st.spinner("Preparing troubleshooting steps..."):
                answer = ask_ai(issue, "IT Support")
            st.markdown(answer)
            st.download_button(
                "Download Troubleshooting Guide",
                answer,
                file_name="it_troubleshooting.txt",
                mime="text/plain"
            )
        else:
            st.warning("Issue ki details enter karein.")


st.divider()
st.caption(
    "AutoMind AI | Python • Streamlit • Gemini • Word • PowerPoint • Excel"
)
