# 🤖 AutoMind AI
### Your Personal AI-Powered Office Productivity & Management Suite

AutoMind AI is a Python and Streamlit-based productivity platform designed to bring AI assistance, office administration, document generation, project planning, workflow tracking, and IT support together in one workspace.

## ✨ Features

- 🤖 **AI Assistant** — Draft content, summarize information, explain concepts, and support everyday office tasks using Google Gemini.
- 📝 **Office Administration** — Prepare professional letters, reports, meeting notes, and other office documents.
- 📄 **Word & PDF Generator** — Create downloadable Word documents and PDF reports.
- 📊 **PowerPoint Maker** — Generate presentation files for meetings, projects, and professional use.
- 📈 **Excel & CSV Tools** — Upload, preview, analyze, and export spreadsheet data.
- 📋 **Workflow Manager** — Create tasks, assign priorities, and track progress.
- 📅 **Project Planner** — Organize project activities and monitor planned work.
- 🛠️ **IT Support Assistant** — Get AI-guided troubleshooting suggestions and technical explanations.
- 📊 **Productivity Dashboard** — Review task status and monitor productivity.

## 🛠️ Technology Stack

- Python
- Streamlit
- Google Gemini API (`google-genai`)
- Pandas
- SQLite
- python-docx
- ReportLab
- OpenPyXL
- python-pptx
- Git and GitHub

## 🚀 Getting Started

### 1. Get the Project

Clone the repository:

```bash
git clone https://github.com/zulqarnainjadoon99/AutoMind-AI.git
cd AutoMind-AI
```

### 2. Install Dependencies

Make sure Python is installed, then run:

```bash
pip install -r requirements.txt
```

### 3. Configure the Gemini API Key

Create a `.streamlit/secrets.toml` file locally with the following content:

```toml
GEMINI_API_KEY = "your-gemini-api-key"
```

Replace the placeholder with your own API key. Never commit this file or publish your actual key.

### 4. Run the Application

```bash
streamlit run app.py
```

Open the local URL displayed in your terminal.

## 🔐 Security & Data Notes

- Keep API keys and other secrets out of source code and public repositories.
- Review AI-generated content before using it professionally.
- SQLite data persistence depends on the hosting environment and storage configuration.
- Do not upload confidential documents or personal information unless you understand how the service processes them.

## 📌 Project Status

AutoMind AI is a personal learning and portfolio project under active development. Features and availability depend on the current application code, installed dependencies, and API configuration.

## 👨‍💻 Author

**Zulqarnain Khan Jadoon**

- GitHub Profile: https://github.com/zulqarnainjadoon99
- Project Repository: https://github.com/zulqarnainjadoon99/AutoMind-AI

---

*Built with Python, Streamlit, and Google Gemini to support smarter office productivity and project coordination.*
