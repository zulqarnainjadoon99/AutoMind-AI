
# 🤖 AutoMind AI
### Your Personal AI-Powered Office Productivity Assistant

AutoMind AI is an AI-powered productivity application built with Python and Streamlit. It brings office productivity, AI assistance, project coordination, document creation, workflow management, and data analysis together in one dashboard.

## ✨ Key Features

- 🤖 **Gemini AI Assistant** — AI-powered writing, summaries, explanations, and everyday office assistance.
- 📝 **Office Administration** — Prepare letters, reports, meeting notes, and professional documents.
- 📄 **Word & PDF Tools** — Generate downloadable Word documents and PDF reports.
- 📊 **PowerPoint Maker** — Create PowerPoint presentation files.
- 📈 **Excel & CSV Tools** — Upload, preview, analyze, and export spreadsheet data.
- 📋 **Workflow Manager** — Create tasks, set priorities, and track completion.
- 📅 **Project Planner** — Organize project activities and planning tasks.
- 🛠️ **IT Support Assistant** — Get AI-guided troubleshooting and technical explanations.
- 📊 **Productivity Dashboard** — Review workflows and monitor task progress.

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

### Run Locally

1. Install Python.
2. Clone this repository or download the source files.
3. Install the dependencies:

   `pip install -r requirements.txt`

4. Configure your Gemini API key securely using Streamlit Secrets or an environment variable.
5. Start the application:

   `streamlit run app.py`

## 🔐 Gemini API Configuration

The AI features require a valid Google Gemini API key.

For Streamlit Cloud, open your app's **Settings → Secrets** and configure:

```toml
GEMINI_API_KEY = "your-gemini-api-key"
```

Replace the placeholder with your own key in Streamlit Secrets. Never publish your actual API key in GitHub or share it publicly.

## 📌 Project Status

AutoMind AI is a personal learning and portfolio project under active development. Available features depend on the current application code and configuration. AI-generated content should be reviewed before professional use.

**Note:** SQLite data may not persist across application restarts or redeployments on cloud hosting unless persistent storage is configured.

## 👨‍💻 Author

**Zulqarnain Khan Jadoon**

GitHub: https://github.com/zulqarnainjadoon99

Project Repository: https://github.com/zulqarnainjadoon99/AutoMind-AI
