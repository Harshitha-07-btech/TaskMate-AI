# TaskMate AI

> A secure, AI-powered task manager built for the hackathon.

## Overview

TaskMate AI helps users manage their tasks, set reminders, and avoid scheduling conflicts — all through a natural-language chat interface powered by an AI backend.

## Key Features

- 🤖 **AI Chat Interface** — Interact with your task list using plain English
- ✅ **Task Tracking** — Create, update, and complete tasks with priorities and due dates
- ⏰ **Reminders** — Get notified before deadlines
- 📅 **Schedule-Conflict Detection** — Automatically detect overlapping tasks and appointments
- 🔒 **ArmorIQ Policy Path** — Gemini task tool calls are routed through ArmorIQ intent tokens; live enforcement requires account setup and MCP registration
- 🗄️ **PostgreSQL / Supabase** — Persistent, scalable database backend
- 🔌 **MCP Server** — Model Context Protocol server exposing task-management tools to the AI

## Tech Stack

| Layer       | Technology                          |
|-------------|-------------------------------------|
| Frontend    | React + Vite                        |
| Backend     | Python + FastAPI                    |
| Database    | PostgreSQL via Supabase             |
| AI Protocol | MCP (Model Context Protocol) Server |
| Security    | ArmorIQ                             |

## Project Structure

```
taskmate-ai/
├── frontend/        # React + Vite application
├── backend/         # Python FastAPI backend
├── mcp-server/      # MCP server & task-management tools
├── .gitignore
└── README.md
```

## Getting Started

> Setup instructions will be added as the project is built out.

## License

MIT
