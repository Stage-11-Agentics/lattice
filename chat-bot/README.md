# Lattice Signal Bot

A Signal group chat bot that interprets natural language messages and executes Lattice task management commands. After write operations, it sends a Mermaid-rendered kanban board image back to the chat.

## Architecture

```
Signal Group Chat
    -> signal-cli-rest-api (Docker)
    -> Bun polling loop
    -> Claude interprets message -> Lattice CLI command
    -> Kanban board image generated (for write commands)
    -> Reply sent to group (text + image)
```

## Prerequisites

- [Bun](https://bun.sh) >= 1.3
- [Docker](https://www.docker.com/) (for signal-cli-rest-api)
- [Lattice](https://github.com/stage11agentics/lattice) CLI installed and accessible
- An Anthropic API key (`ANTHROPIC_API_KEY` env var)
- A Signal phone number for the bot

## Setup

### 1. Start signal-cli-rest-api

```bash
cd signal-bot
docker compose up -d
```

### 2. Register/link a Signal phone number

Register a new number:
```bash
curl -X POST "http://localhost:8080/v1/register/+1234567890"
```

Or link to an existing Signal account by scanning a QR code:
```bash
curl "http://localhost:8080/v1/qrcodelink?device_name=lattice-bot"
```

### 3. Get your group ID

Once linked, receive a message in the target group, then check:
```bash
curl "http://localhost:8080/v1/receive/+1234567890" | jq '.[].envelope.dataMessage.groupInfo.groupId'
```

### 4. Configure the bot

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your:
- Signal phone number
- Group ID(s)
- Lattice project root path
- LLM model (defaults to claude-sonnet-4-5)

### 5. Install dependencies

```bash
bun install
```

### 6. Run the bot

```bash
ANTHROPIC_API_KEY=sk-ant-... bun run src/index.ts
```

Or with a custom config path:
```bash
ANTHROPIC_API_KEY=sk-ant-... bun run src/index.ts /path/to/config.yaml
```

## Usage

In your Signal group, prefix messages with `@lattice` or `/lat`:

| Message | Action |
|---------|--------|
| `@lattice create a bug for login timeout, high priority` | Creates a task |
| `@lattice list in-progress tasks` | Lists filtered tasks |
| `@lattice show LAT-42` | Shows task details |
| `@lattice move LAT-15 to review` | Changes task status |
| `@lattice assign LAT-10 to alice` | Assigns task |
| `@lattice complete LAT-7` | Marks task done |
| `@lattice weather` | Project health digest |
| `@lattice stats` | Project statistics |
| `@lattice help` | Shows available commands |

After any write command (create, status, assign, complete, update), the bot also sends a kanban board image showing the current board state.

Messages without the trigger prefix are ignored.

## Development

```bash
bun --watch run src/index.ts  # Auto-restart on file changes
```
