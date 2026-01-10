# MyFitnessPal MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that enables AI assistants like Claude to interact with your MyFitnessPal data, including food diary, exercises, body measurements, nutrition goals, and water intake.

## Features

| Tool | Type | Description |
|------|------|-------------|
| `mfp_get_diary` | Read | Get food diary entries for any date |
| `mfp_search_food` | Read | Search the MyFitnessPal food database |
| `mfp_get_food_details` | Read | Get detailed nutrition info for a food item |
| `mfp_get_measurements` | Read | Get weight/body measurement history |
| `mfp_set_measurement` | Write | Log a new weight or body measurement |
| `mfp_get_exercises` | Read | Get logged exercises (cardio & strength) |
| `mfp_get_goals` | Read | Get daily nutrition goals |
| `mfp_set_goals` | Write | Update daily nutrition goals |
| `mfp_get_water` | Read | Get water intake for a date |
| `mfp_get_report` | Read | Get nutrition reports over a date range |

## Prerequisites

- **Python 3.10+**
- **MyFitnessPal account**
- **One of the following for authentication:**
  - Your MFP username/email and password (recommended), OR
  - Chrome or Firefox with an active MyFitnessPal login session

### Authentication Options

This MCP supports multiple authentication methods:

| Method | Setup | Persistence |
|--------|-------|-------------|
| **Credentials in config** | Add `MFP_USERNAME` and `MFP_PASSWORD` to Claude Desktop config | Automatic (session cached 30 days) |
| **Browser cookies** | Log into myfitnesspal.com in Chrome/Firefox | Until browser session expires |

## Installation

### Option 1: Install from Source

```bash
# Clone the repository
git clone https://github.com/yourusername/myfitnesspal-mcp-python.git
cd myfitnesspal-mcp-python

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: .\venv\Scripts\activate

# Install the package
pip install -e .
```

### Option 2: Install with pip

```bash
pip install mfp-mcp
```

## Configuration for Claude Desktop

### Step 1: Locate Your Config File

| OS | Config File Location |
|----|---------------------|
| **macOS** | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Windows** | `%APPDATA%\Claude\claude_desktop_config.json` |

### Step 2: Add the MCP Server Configuration

If the file doesn't exist, create it. Add or merge the following configuration:

#### Option A: With Credentials (Recommended - No Browser Required)

**macOS Example:**
```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "/Users/yourname/myfitnesspal-mcp-python/venv/bin/python",
      "args": ["-m", "mfp_mcp.server"],
      "env": {
        "MFP_USERNAME": "your_email@example.com",
        "MFP_PASSWORD": "your_password"
      }
    }
  }
}
```

**Windows Example:**
```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "C:\\Users\\YourName\\myfitnesspal-mcp-python\\venv\\Scripts\\python.exe",
      "args": ["-m", "mfp_mcp.server"],
      "env": {
        "MFP_USERNAME": "your_email@example.com",
        "MFP_PASSWORD": "your_password"
      }
    }
  }
}
```

#### Option B: Without Credentials (Browser Cookie Fallback)

**macOS Example:**
```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "/Users/yourname/myfitnesspal-mcp-python/venv/bin/python",
      "args": ["-m", "mfp_mcp.server"]
    }
  }
}
```

> ⚠️ **Important**: Use **full absolute paths** to the Python executable in your virtual environment. Replace `yourname`/`YourName` with your actual username.

### Step 3: Restart Claude Desktop

After saving the config file, **completely quit and restart Claude Desktop** for the changes to take effect.

### Step 4: Verify Connection

In Claude Desktop, you should see a hammer icon (🔨) indicating MCP tools are available. Try asking:

> "Show my MyFitnessPal diary for today"

## Authentication Methods

The MCP server supports three authentication methods, tried in this order:

### 1. Environment Variables (Recommended)
Set `MFP_USERNAME` and `MFP_PASSWORD` in your Claude Desktop config's `env` section. This is the most reliable method and doesn't require a browser.

```json
"env": {
  "MFP_USERNAME": "your_email@example.com",
  "MFP_PASSWORD": "your_password"
}
```

### 2. Stored Session Cookies
After successful authentication, session cookies are saved to `~/.mfp_mcp/cookies.json`. These persist for 30 days, so you won't need to re-authenticate frequently.

### 3. Browser Cookies (Fallback)
If no credentials are provided and no stored cookies exist, the server falls back to reading cookies from Chrome or Firefox. You must be logged into myfitnesspal.com in your browser.

## Security Note on Credentials

Your MyFitnessPal credentials in the Claude Desktop config are stored locally on your machine. The config file is only readable by your user account. However, if you're concerned about storing credentials:

1. Use Option B (browser cookies) instead
2. Or use a dedicated MyFitnessPal account for API access
3. Session cookies are stored in `~/.mfp_mcp/cookies.json` with restricted permissions

## Usage Examples

Once configured, you can interact with your MyFitnessPal data through Claude:

### Food Diary
```
"Show me what I ate today"
"Get my food diary for 2026-01-05"
"What meals did I log yesterday?"
```

### Track Weight Progress
```
"Show my weight history for the past 30 days"
"Log my weight as 232.5 pounds"
"What's my weight trend this month?"
```

### Search Foods
```
"Search MyFitnessPal for chicken breast"
"Find nutrition info for Greek yogurt"
"Look up calories in a banana"
```

### Check Goals vs Actual
```
"Compare my nutrition goals to what I actually ate today"
"Am I on track with my protein intake?"
"How many calories do I have left today?"
```

### Exercise Log
```
"What exercises did I log today?"
"Show my workout from yesterday"
```

### Nutrition Reports
```
"Show my calorie intake over the past week"
"What's my average protein intake this week?"
"Generate a nutrition report for January"
```

## Project Structure

```
myfitnesspal-mcp-python/
├── Dockerfile              # Container deployment
├── pyproject.toml          # Package configuration
├── README.md               # This file
└── src/
    └── mfp_mcp/
        ├── __init__.py     # Package initialization
        └── server.py       # MCP server implementation
```

## Development

### Setup Development Environment

```bash
# Clone and enter directory
git clone https://github.com/yourusername/myfitnesspal-mcp-python.git
cd myfitnesspal-mcp-python

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest
```

### Code Formatting

```bash
black src/
isort src/
ruff check src/
```

### Type Checking

```bash
mypy src/
```

## Docker Deployment

> ⚠️ **Note**: Docker deployment requires mounting your browser's cookie database for authentication.

```bash
# Build the image
docker build -t mfp-mcp .

# Run with Chrome cookies mounted (Linux example)
docker run -it --rm \
  -v ~/.config/google-chrome:/root/.config/google-chrome:ro \
  mfp-mcp
```

## Troubleshooting

### "Failed to authenticate with MyFitnessPal"

**Problem**: The server can't read your browser cookies.

**Solutions**:
1. Make sure you're logged into myfitnesspal.com in Chrome or Firefox
2. Try logging out and back in to MyFitnessPal
3. Clear browser cookies and log in fresh
4. On **macOS**, grant **Full Disk Access** to your terminal/IDE:
   - System Preferences → Security & Privacy → Privacy → Full Disk Access
   - Add Terminal.app or your IDE

### "No module named 'mfp_mcp'"

**Problem**: Package not installed or wrong Python environment.

**Solutions**:
1. Ensure you're using the correct Python from your virtual environment
2. Reinstall the package: `pip install -e .`
3. Verify the path in your Claude Desktop config points to the venv Python

### Tools not appearing in Claude Desktop

**Problem**: MCP server not connecting.

**Solutions**:
1. Check the config file syntax (valid JSON)
2. Use absolute paths in the configuration
3. Restart Claude Desktop completely (quit and relaunch)
4. Check Claude Desktop logs for errors

### Empty responses or no data

**Problem**: Authentication works but no data returned.

**Solutions**:
1. Verify you have data logged in MyFitnessPal for the requested date
2. Check the date format (YYYY-MM-DD)
3. Try a recent date where you know you have entries

## API Reference

### mfp_get_diary
Get food diary for a specific date.
- `date` (optional): YYYY-MM-DD format, defaults to today
- `response_format`: "markdown" or "json"

### mfp_search_food
Search the MyFitnessPal food database.
- `query` (required): Search term
- `limit` (optional): Max results (default 10, max 50)
- `response_format`: "markdown" or "json"

### mfp_get_food_details
Get detailed nutrition for a food item.
- `mfp_id` (required): MyFitnessPal food ID from search results
- `response_format`: "markdown" or "json"

### mfp_get_measurements
Get body measurement history.
- `measurement` (optional): "Weight", "Body Fat", "Waist", etc.
- `start_date` (optional): YYYY-MM-DD (default 30 days ago)
- `end_date` (optional): YYYY-MM-DD (default today)
- `response_format`: "markdown" or "json"

### mfp_set_measurement
Log a body measurement for today.
- `measurement` (optional): Type (default "Weight")
- `value` (required): Numeric value

### mfp_get_exercises
Get exercise log for a date.
- `date` (optional): YYYY-MM-DD (default today)
- `response_format`: "markdown" or "json"

### mfp_get_goals
Get daily nutrition goals.
- `date` (optional): YYYY-MM-DD (default today)
- `response_format`: "markdown" or "json"

### mfp_set_goals
Update nutrition goals.
- `calories` (optional): Daily calorie goal
- `protein` (optional): Daily protein in grams
- `carbohydrates` (optional): Daily carbs in grams
- `fat` (optional): Daily fat in grams

### mfp_get_water
Get water intake for a date.
- `date` (optional): YYYY-MM-DD (default today)

### mfp_get_report
Get nutrition report over a date range.
- `report_name` (optional): "Net Calories", "Protein", "Fat", "Carbs"
- `start_date` (optional): YYYY-MM-DD (default 7 days ago)
- `end_date` (optional): YYYY-MM-DD (default today)
- `response_format`: "markdown" or "json"

## Security & Privacy

- **Browser Cookies**: This server reads your browser cookies to authenticate with MyFitnessPal. No credentials are stored by this server.
- **Local Only**: The server runs locally on your machine via stdio transport.
- **No External Transmission**: Your MyFitnessPal data is only transmitted between your computer and MyFitnessPal's servers.

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Acknowledgments

- [python-myfitnesspal](https://github.com/coddingtonbear/python-myfitnesspal) - The underlying library for MyFitnessPal access
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) - Model Context Protocol framework
- [Anthropic](https://anthropic.com) - Claude and the MCP specification
