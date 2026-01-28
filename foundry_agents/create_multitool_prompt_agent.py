import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.projects.models import (
    PromptAgentDefinition,
    MicrosoftFabricAgentTool,
    FabricDataAgentToolParameters,
    ToolProjectConnection,
)
from dotenv import load_dotenv

load_dotenv()

foundry_account_name = os.getenv("foundry_account_name")
foundry_project_name = os.getenv("foundry_project_name")
foundry_rg = os.getenv("foundry_resource_group")
subscription_id = os.getenv("foundry_subscription_id")
agent_name = os.getenv("agent_name")
fabrric_data_mcp_tool_name = os.getenv("fabrric_data_mcp_tool_name")
mcp_tool_server_label = os.getenv("mcp_tool_server_name")
mcp_tool_server_url = os.getenv("mcp_tool_server_url")

if not foundry_account_name or not foundry_project_name or not foundry_rg \
    or not subscription_id or not agent_name \
    or not fabrric_data_mcp_tool_name or not mcp_tool_server_label \
    or not mcp_tool_server_url:
    print("Please set the environment variables in the .env file.")
    exit(1)

fabric_data_agent_connection_id = (
    f"/subscriptions/{subscription_id}/resourceGroups/{foundry_rg}"
    f"/providers/Microsoft.CognitiveServices/accounts/{foundry_account_name}"
    f"/projects/{foundry_project_name}/connections/{fabrric_data_mcp_tool_name}"
)

mcp_tool_project_connection_id=(
    f"/subscriptions/{subscription_id}/resourceGroups/{foundry_rg}"
    f"/providers/Microsoft.CognitiveServices/accounts/{foundry_account_name}"
    f"/projects/{foundry_project_name}/connections/{mcp_tool_server_label}")


print("fabric_data_agent_connection_id:", fabric_data_agent_connection_id)
print("mcp_tool_project_connection_id:", mcp_tool_project_connection_id)

# Initialize the client
client = AIProjectClient(
    endpoint=f"https://{foundry_account_name}.services.ai.azure.com/api/projects/{foundry_project_name}",
    credential=DefaultAzureCredential()
)


#You are Jira and Confluence assistant and Fabric Data Agent assistant. 
#        You can answer questions related to Jira, Confluence and Fabric Data as per below.
#        Jira Issues: Use the attached Jira MCP tool to fetch Jira issues and provide relevant information.
#        Confluence Content: Use the attached Confluence MCP tool to lookup Confluence content pages and provide relevant information.

agent = client.agents.create_version(
    agent_name=agent_name,
    definition=PromptAgentDefinition(
        model="gpt-4.1-mini",
        instructions=""" 
        You are an assistant for Jira, Confluence and Invoice Data.
        Use the tools provided to you to answer user questions.
        For questions about Jira Issues: Use the attached Jira MCP tool to fetch Jira issues and provide relevant information.
        For questions about Confluence Content: Use the attached Confluence MCP tool to lookup Confluence content pages and provide relevant information.
        For questions about Invoice Data: Use the attached Fabric Data MCP tool to query invoice data and provide relevant information.

        Answer questions only for the above domains only the tools provide the relevant data. If the question is outside these domains, respond with "I am sorry, I cannot assist with that."
        """,
        tools=[
            {
                "type": "mcp",
                "server_label": mcp_tool_server_label,
                "server_url": mcp_tool_server_url,
                "project_connection_id": mcp_tool_project_connection_id
            },
            MicrosoftFabricAgentTool(
                fabric_dataagent_preview=FabricDataAgentToolParameters(
                    project_connections=[
                        ToolProjectConnection(project_connection_id=fabric_data_agent_connection_id)
                    ]
                )
            )
        ],
    ),
)
print(f"Agent created (id: {agent.id}, name: {agent.name}, version: {agent.version})")

