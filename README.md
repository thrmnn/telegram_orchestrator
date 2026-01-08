# Telegram Development Orchestrator

A powerful system for orchestrating multiple Claude Code agents for autonomous software development, controlled entirely via Telegram from your mobile phone.

## Features

- **Telegram Bot Interface**: Natural language task input via text or voice messages
- **Multi-Agent Orchestration**: Manage multiple Claude Code instances working in parallel
- **Intelligent Task Distribution**: Priority-based queue with dependency tracking
- **Decision Escalation**: Smart detection of when human input is needed
- **Git Workflow Automation**: Automatic branch creation, commits, and PR generation
- **Real-time Notifications**: Task status, errors, PRs, and decisions delivered to your phone
- **Budget Protection**: Daily spending limits with proactive warnings

## Quick Start

### Prerequisites

- Python 3.11+
- Docker and Docker Compose (recommended)
- Telegram Bot Token (create via [@BotFather](https://t.me/botfather))
- GitHub Personal Access Token (for PR operations)
- Anthropic API Key (for Claude Code)

### Installation

1. **Clone and setup**:
```bash
git clone <repository-url>
cd telegram-orchestrator
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

2. **Configure environment**:
Edit `.env` with your credentials:
```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_AUTHORIZED_CHAT_IDS=your_telegram_user_id
GITHUB_TOKEN=your_github_token
ANTHROPIC_API_KEY=your_anthropic_key
```

3. **Start with Docker**:
```bash
docker-compose up -d
```

Or **run locally**:
```bash
pip install -r requirements.txt
python main.py
```

### Getting Your Telegram Chat ID

1. Start a chat with [@userinfobot](https://t.me/userinfobot)
2. It will respond with your user ID
3. Add this ID to `TELEGRAM_AUTHORIZED_CHAT_IDS`

## Usage

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/create <task>` | Create a new development task |
| `/status` | View system status |
| `/agents` | List all agents and their status |
| `/review` | View pending pull requests |
| `/approve <task_id>` | Approve and merge a PR |
| `/pause [task_id]` | Pause task(s) |
| `/cancel <task_id>` | Cancel a task |
| `/logs [task_id]` | View activity logs |
| `/setmode <mode>` | Set autonomy mode |
| `/setbudget <usd>` | Set daily budget |
| `/help` | Show help message |

### Quick Task Creation

Simply send a text message to create a task:
```
Add user authentication with JWT tokens
```

Or send a voice message describing your task!

### Priority Tags

Add priority tags to your task descriptions:
- `[urgent]` - Highest priority
- `[high]` - High priority
- `[low]` - Low priority

Example:
```
/create [urgent] Fix the login bug causing 500 errors
```

### Autonomy Modes

| Mode | Description |
|------|-------------|
| `full-auto` | Agents work autonomously, only escalate critical decisions |
| `balanced` | Ask for major architectural/security decisions |
| `supervised` | Ask before each significant step |

Set via:
```
/setmode balanced
```

## Architecture

```
telegram-orchestrator/
├── bot/                    # Telegram bot handlers
│   ├── handlers.py         # Command handlers
│   ├── notifications.py    # Proactive messaging
│   └── auth.py            # User authentication
├── orchestrator/           # Core orchestration logic
│   ├── engine.py          # Main orchestration engine
│   ├── task_queue.py      # Redis-backed task queue
│   ├── agent_manager.py   # Agent pool management
│   └── decision_engine.py # Escalation logic
├── agents/                 # Claude Code wrapper
│   ├── claude_wrapper.py  # CLI interface
│   ├── workspace.py       # Workspace isolation
│   └── monitor.py         # Progress tracking
├── git_ops/               # Git operations
│   ├── branch_manager.py  # Branch management
│   └── pr_handler.py      # GitHub PR operations
├── models/                # Database models
│   ├── database.py        # SQLAlchemy models
│   └── schemas.py         # Pydantic schemas
└── config/                # Configuration
    └── settings.py        # Settings management
```

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token | Yes |
| `TELEGRAM_AUTHORIZED_CHAT_IDS` | Comma-separated list of authorized user IDs | Yes |
| `GITHUB_TOKEN` | GitHub Personal Access Token | For PR features |
| `GITHUB_REPO` | Default repository (owner/repo) | For PR features |
| `ANTHROPIC_API_KEY` | Anthropic API key | Yes |
| `DATABASE_URL` | Database connection URL | No (defaults to SQLite) |
| `REDIS_URL` | Redis connection URL | No (uses in-memory queue) |
| `AGENT_COUNT` | Number of parallel agents | No (default: 3) |
| `DAILY_BUDGET_USD` | Daily spending limit | No (default: 50) |
| `AUTONOMY_MODE` | Default autonomy mode | No (default: balanced) |

### config.yaml

See `config/config.example.yaml` for a complete configuration example.

## Decision Escalation

The system automatically detects situations requiring human input:

- **Security decisions**: API keys, authentication, authorization
- **Breaking changes**: API changes, schema modifications
- **Database migrations**: Table alterations, data migrations
- **Architectural decisions**: Major refactors, design patterns
- **Ambiguous requirements**: Multiple valid approaches

When escalation occurs, you receive a Telegram notification with options to respond.

## Notifications

The bot sends proactive notifications for:

- Task started/completed/failed
- Decision points requiring input
- Pull requests ready for review
- Budget warnings
- Agent errors
- Daily digests

## Development

### Local Development Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Run locally (uses SQLite and in-memory queue)
python main.py
```

### Running Tests

```bash
pytest tests/ -v
```

### Database Migrations

Using Alembic for migrations:
```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```

## Monitoring

### System Status

Use `/status` to see:
- Active/idle agents
- Pending/in-progress tasks
- Daily cost and budget usage
- Pending decisions

### Logs

View activity logs with `/logs` or check `logs/orchestrator.log`.

## Troubleshooting

### Bot not responding

1. Check `TELEGRAM_BOT_TOKEN` is correct
2. Verify your chat ID is in `TELEGRAM_AUTHORIZED_CHAT_IDS`
3. Check logs for errors

### Tasks not executing

1. Ensure Claude Code CLI is installed and configured
2. Check `ANTHROPIC_API_KEY` is set
3. Verify agents are initialized (`/agents`)

### PR creation failing

1. Verify `GITHUB_TOKEN` has repo access
2. Check `GITHUB_REPO` is correctly formatted (owner/repo)
3. Ensure branch has been pushed

## Security Considerations

- Only authorized Telegram users can control the bot
- GitHub tokens should have minimal required permissions
- Use environment variables for all secrets
- Consider running in isolated Docker network
- Enable Telegram bot privacy mode if not needed

## License

MIT License - see LICENSE file for details.

## Contributing

Contributions welcome! Please read CONTRIBUTING.md before submitting PRs.
