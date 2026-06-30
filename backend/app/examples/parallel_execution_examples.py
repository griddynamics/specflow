"""
Example usage of parallel execution with YAML-configured workspaces.
"""

import asyncio
import logging

from app.core.config import settings
from app.services.claude_code import agent_query
from app.services.parallel_executor import ParallelAgentExecutor, execute_query_parallel
from app.services.workspace_config_loader import WorkspaceConfigLoader
from app.services.workspace_manager import WorkspaceManager


# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def example_1_simple_parallel_query():
    """
    Example 1: Simple parallel query across 3 workspaces from YAML config.
    """
    logger.info("=== Example 1: Simple Parallel Query ===")
    
    # Load workspace configurations from YAML
    config_loader = WorkspaceConfigLoader(
        config_path="backend/app/config/workspaces.example.yaml",
        logger=logger
    )
    workspaces = config_loader.get_workspaces()
    
    logger.info(f"Loaded {len(workspaces)} workspaces:")
    for i, ws in enumerate(workspaces):
        logger.info(f"  {i+1}. {ws.provider}/{ws.model} -> {ws.workspace_path}")
    
    # Create workspace manager
    workspace_manager = WorkspaceManager(settings, logger)
    
    # Execute same query across all workspaces in parallel
    results = await execute_query_parallel(
        workspaces=workspaces,
        workspace_manager=workspace_manager,
        system_prompt="Print the first 10 prime numbers",
        logger=logger,
        max_turns=1
    )
    
    # Process results
    for result in results:
        print(result)
        if result.success:
            logger.info(
                f"✅ [{result.workspace_name}] Success ({result.duration_ms:.0f}ms): "
                f"{result.result.result[:100]}..."
            )
        else:
            logger.error(
                f"❌ [{result.workspace_name}] Failed: {result.error}"
            )


async def example_2_custom_parallel_execution():
    """
    Example 2: Custom parallel execution with different prompts per workspace.
    """
    logger.info("=== Example 2: Custom Parallel Execution ===")
    
    # Load workspaces
    config_loader = WorkspaceConfigLoader("app/config/workspaces.yaml", logger)
    workspaces = config_loader.get_workspaces()
    workspace_manager = WorkspaceManager(settings, logger)
    
    # Create custom agent function with different prompts
    prompts = [
        "Focus on code quality and maintainability",
        "Focus on performance optimization",
        "Focus on security vulnerabilities"
    ]
    
    async def custom_agent_fn(workspace, manager, log, prompt):
        return await agent_query(
            system_prompt=prompt,
            workspace_path=str(workspace.get_full_path()),
            workspace_settings=workspace,
            workspace_manager=manager,
            logger=log,
            max_turns=10
        )
    
    # Execute in parallel with different prompts
    tasks = []
    for workspace, prompt in zip(workspaces, prompts):
        task = custom_agent_fn(workspace, workspace_manager, logger, prompt)
        tasks.append(task)
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"❌ Workspace {i} failed: {result}")
        else:
            logger.info(f"✅ Workspace {i} completed: {result.result[:100]}...")


async def example_3_selective_workspace_execution():
    """
    Example 3: Execute only specific workspaces by name.
    """
    logger.info("=== Example 3: Selective Workspace Execution ===")
    
    # Load config
    config_loader = WorkspaceConfigLoader("app/config/workspaces.yaml", logger)
    
    # Get specific workspaces
    claude_workspace = config_loader.get_workspace("workspace-claude")
    gemini_workspace = config_loader.get_workspace("workspace-gemini")
    
    if not claude_workspace or not gemini_workspace:
        logger.error("Required workspaces not found in config")
        return
    
    workspaces = [claude_workspace, gemini_workspace]
    workspace_manager = WorkspaceManager(settings, logger)
    
    # Execute only these two workspaces
    results = await execute_query_parallel(
        workspaces=workspaces,
        workspace_manager=workspace_manager,
        system_prompt="Compare architectural patterns",
        logger=logger
    )
    
    # Compare results
    logger.info("\n=== Comparison ===")
    for result in results:
        logger.info(f"\n{result.workspace_name}:")
        logger.info(f"  Provider: {result.workspace_settings.provider}")
        logger.info(f"  Model: {result.workspace_settings.model}")
        logger.info(f"  Duration: {result.duration_ms:.0f}ms")
        logger.info(f"  Result: {result.result.result[:200]}..." if result.success else f"  Error: {result.error}")


async def example_4_with_executor_class():
    """
    Example 4: Using ParallelAgentExecutor class directly for more control.
    """
    logger.info("=== Example 4: Using ParallelAgentExecutor ===")
    
    # Load workspaces
    config_loader = WorkspaceConfigLoader("app/config/workspaces.yaml", logger)
    workspaces = config_loader.get_workspaces()
    workspace_names = config_loader.list_workspace_names()
    
    # Create executor
    workspace_manager = WorkspaceManager(settings, logger)
    executor = ParallelAgentExecutor(workspace_manager, logger)
    
    # Define custom agent function
    async def my_agent_function(workspace, manager, log):
        log.info(f"Processing {workspace.provider}/{workspace.model}")
        
        result = await agent_query(
            system_prompt=f"Analyze code with focus on {workspace.provider} best practices",
            workspace_path=str(workspace.get_full_path()),
            workspace_settings=workspace,
            workspace_manager=manager,
            logger=log,
            max_turns=5
        )
        
        return result
    
    # Execute in parallel
    results = await executor.execute_parallel(
        workspaces=workspaces,
        agent_fn=my_agent_function,
        workspace_names=workspace_names
    )
    
    # Aggregate results
    total_duration = sum(r.duration_ms for r in results if r.duration_ms)
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    
    logger.info("\n=== Summary ===")
    logger.info(f"Total workspaces: {len(results)}")
    logger.info(f"Successful: {len(successful)}")
    logger.info(f"Failed: {len(failed)}")
    logger.info(f"Total duration: {total_duration:.0f}ms")
    logger.info(f"Average duration: {total_duration/len(results):.0f}ms")


async def main():
    """Run all examples."""

    
    # Run examples one by one
    await example_1_simple_parallel_query()
    # print("\n" + "="*80 + "\n")
    
    # await example_2_custom_parallel_execution()
    # print("\n" + "="*80 + "\n")
    
    # await example_3_selective_workspace_execution()
    # print("\n" + "="*80 + "\n")
    
    # await example_4_with_executor_class()


if __name__ == "__main__":
    asyncio.run(main())
