# 🤖 AutoMind AI

**Your Personal AI-Powered Office Productivity Assistant**

AutoMind AI is a Python-based productivity application designed to simplify everyday office work. It combines workflow management, AI-powered assistance, project coordination, document preparation, IT support, and data reporting in one interactive dashboard.

## ✨ Features

- **AI Assistant:** Get AI-powered help with everyday office tasks and professional writing.
- **Workflow Management:** Create, organize, prioritize, and track workflows.
- **Office Administration:** Draft letters, reports, summaries, and other office documents.
- **Project Coordination:** Organize project activities and prepare project plans.
- **IT Support:** Get troubleshooting guidance and manage helpdesk tasks.
- **Data Analysis:** Upload CSV files, preview data, and request AI-assisted analysis.
- **Report Export:** Download supported reports and data in convenient formats.
- **Interactive Dashboard:** Monitor tasks and progress in a simple interface.

## 🛠️ Technologies Used

- Python
- Streamlit
- Google Gemini API
- Pandas
- SQLite
- Python-docx
- ReportLab
- OpenPyXL
- Git and GitHub

## 🚀 Getting Started

### Run Locally

1. Install Python.
2. Clone or download this repository.
3. Install the required dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure your Gemini API key securely using an environment variable or Streamlit Secrets. Never publish your API key in the repository.
5. Start the application:

   ```bash
   streamlit run app.py
   ```

## 🔐 API Configuration

The AI assistant requires a valid Google Gemini API key. Configure it securely in Streamlit Secrets:

```toml
GEMINI_API_KEY = "your-gemini-api-key"
```

Replace the placeholder with your own API key. Keep the actual key private and never commit it to GitHub.

## 📌 Project Status

AutoMind AI is a personal learning and portfolio project under development. Features and integrations may evolve as the project improves. AI-generated results should be reviewed before professional use.

**Note:** Local database files and application data may not persist across restarts on some cloud hosting platforms unless persistent storage is configured.

## 👨‍💻 Author

**Zulqarnain Khan Jadoon**

GitHub: https://github.com/zulqarnainjadoon99

Repository: https://github.com/zulqarnainjadoon99/AutoMind-AI
