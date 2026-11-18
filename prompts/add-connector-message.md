Add a stdio MCP connector named <your-connector-id> with this command:

sh -c 'exec npx -y mcp-remote https://<your-host>:10000/mcp --transport http-only --header "Authorization:Bearer ${<NAME>_MCP_BEARER}"'

(Use your Funnel port instead of 10000, and omit the port if it is 443.)

Ask me for <NAME>_MCP_BEARER with a masked secret request, and store it as a box environment variable. Never put the bearer value in chat. Then restrict the connector to this bot with SetMcpInstructions using this connector instruction:

This connector is the <your-bot-name> Slack app (bot @dashhq_pm) and belongs only to the <your-bot-name> bot. Every other bot (Dob, Landing, Reports, Personal, Forge PM, Yappr PM, Test) must never use it. <your-bot-name> uses it to read Slack context and to reply only in the thread or DM where it was pinged, per its Slack pings routine.

After a server-side tool change you may need RestartMcpServers or a new turn before the new tool list appears. Stale local mcp-remote processes can keep serving an old tool list — kill them and restart if the tools look wrong.
