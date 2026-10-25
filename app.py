
import streamlit as st
import pandas as pd
from datetime import datetime

st.set_page_config(
    page_title="AutoMind AI",
    page_icon="🤖",
    layout="wide"
)

st.title("🤖 AutoMind AI")
st.caption("Your Personal Workflow & Task Management Assistant")

if "tasks" not in st.session_state:
    st.session_state.tasks = []

st.sidebar.header("Create a Workflow")

workflow_name = st.sidebar.text_input("Workflow Name")

category = st.sidebar.selectbox(
    "Category",
    [
        "Project Coordination",
        "IT Support",
        "Office Administration",
        "Report Preparation",
        "General Tasks"
    ]
)

priority = st.sidebar.selectbox(
    "Priority",
    ["Low", "Medium", "High"]
)

description = st.sidebar.text_area("Description")

if st.sidebar.button("Create Workflow", type="primary"):
    if workflow_name.strip():
        st.session_state.tasks.append({
            "Workflow": workflow_name.strip(),
            "Category": category,
            "Priority": priority,
            "Description": description,
            "Status": "Pending",
            "Created": datetime.now().strftime(
                "%Y-%m-%d %H:%M"
            )
        })
        st.sidebar.success("Workflow created!")
    else:
        st.sidebar.warning("Please enter a workflow name.")

tasks = st.session_state.tasks
total = len(tasks)

completed = sum(
    task["Status"] == "Completed"
    for task in tasks
)

pending = total - completed
progress = completed / total if total else 0

col1, col2, col3 = st.columns(3)

col1.metric("Total Workflows", total)
col2.metric("Completed", completed)
col3.metric("Pending", pending)

st.subheader("📋 Your Workflows")

st.progress(
    progress,
    text=f"Overall progress: {progress:.0%}"
)

if tasks:
    for index, task in enumerate(tasks):
        with st.expander(
            f"{'✅' if task['Status'] == 'Completed' else '🕒'} "
            f"{task['Workflow']} — {task['Priority']} Priority"
        ):
            st.write("**Category:**", task["Category"])
            st.write(
                "**Description:**",
                task["Description"] or "No description"
            )
            st.write("**Created:**", task["Created"])

            new_status = st.selectbox(
                "Status",
                ["Pending", "Completed"],
                index=0 if task["Status"] == "Pending" else 1,
                key=f"status_{index}"
            )

            st.session_state.tasks[index]["Status"] = new_status

            if st.button(
                "Delete Workflow",
                key=f"delete_{index}"
            ):
                st.session_state.tasks.pop(index)
                st.rerun()

    st.subheader("📤 Export Your Data")

    df = pd.DataFrame(st.session_state.tasks)

    csv_data = df.to_csv(index=False).encode("utf-8")

    st.download_button(
        "Download CSV Report",
        data=csv_data,
        file_name="automind_workflows.csv",
        mime="text/csv"
    )
else:
    st.info(
        "No workflows yet. Create your first workflow "
        "using the sidebar."
    )

st.divider()

st.caption(
    "AutoMind AI | Personal Portfolio Project | "
    "Built with Python and Streamlit"
)

