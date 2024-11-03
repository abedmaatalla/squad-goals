import datetime
import json
import re
from copy import copy
from typing import List, Dict, Tuple

from .llms.base_llm import LLM
from .task import Task
from .tools.base_tool import BaseTool, ReturnFinalAnswerTool

OBSERVATION_TOKEN = "Observation:"
THOUGHT_TOKEN = "Thought:"
PROMPT_TEMPLATE = """Today is {today} and you can use tools to get new information. 
Respond to the user's input as best as you can using the following tools:


{tool_description}

First Thought:
Thought: comment on what you want to do next.
Action: the action to take, exactly one element of [{tool_names}]
Action Input: the input to the action (must be a json loadable dictionary of parameters e.g. {{{{"param": "value"}}}})
Observation: the result of the action
Next Thought:
Thought: Now comment on what you want to do next.
Action: the next action to take, exactly one element of [{tool_names}]
Action Input: the input to the next action (must be a json loadable dictionary of parameters e.g. {{{{"param": "value"}}}})
Observation: the result of the next action
... (this Thought/Action/Action Input/Observation repeats until you are sure of the answer)
Next Thought:
Thought: Now comment on what you want to do next.
Action: the next action to take, exactly one element of [{tool_names}]
Action Input: the input to the next action (must be a json loadable dictionary of parameters e.g. {{{{"param": "value"}}}})
Observation: the result of the next action
Next Thought:
Thought: I can finally return the final answer
Action: Return Final Answer Tool
Action Input: The final answer to the task

Begin:

{goal}

First Thought:
{previous_responses}
"""


class Agent():
    def __init__(self, llm: LLM, tools: List[BaseTool],
                 prompt_template: str = PROMPT_TEMPLATE,
                 max_loops: int = 5,
                 stop_pattern: List[str] = [f'\n{OBSERVATION_TOKEN}', f'\n\t{OBSERVATION_TOKEN}'],
                 verbose: bool = False,
                 debug: bool = False
                 ):
        self.llm = llm
        self.tools = tools
        if not any(isinstance(tool, ReturnFinalAnswerTool) for tool in tools):
            tools.append(ReturnFinalAnswerTool())
        self.prompt_template = copy(prompt_template)
        self.max_loops = max_loops
        self.stop_pattern = stop_pattern
        self.ai_responses = []
        self.verbose = verbose
        self.debug = debug
        self.errors_encountered = []

    @property
    def tool_description(self) -> str:
        return "\n".join(
            [f"{tool.name}: {tool.description}. how to run: {tool._describe_run()}" for tool in self.tools])

    @property
    def tool_names(self) -> str:
        return ", ".join([tool.name for tool in self.tools])

    @property
    def tool_by_names(self) -> Dict[str, BaseTool]:
        return {tool.name: tool for tool in self.tools}

    def run(self, task: Task):
        previous_responses = copy(self.ai_responses)
        num_loops = 0
        prompt = copy(self.prompt_template).format(
            today=datetime.date.today(),
            tool_description=self.tool_description,
            tool_names=self.tool_names,
            goal=task.goal,
            previous_responses='{previous_responses}'
        )
        while num_loops < self.max_loops:
            num_loops += 1
            curr_prompt = prompt.format(previous_responses='\n'.join(previous_responses).strip())
            generated, tool, tool_input = self.decide_next_action(curr_prompt)
            if self.verbose:
                print('------')
                print('CURR PROMPT')
                print('------')
                print(curr_prompt)
                print('------')
                print('------')
                print('RAW GENERATED')
                print('------')
                print(generated)
                print('------')
            if tool not in self.tool_by_names:
                raise ValueError(f"Unknown tool: {tool}")
            if self.verbose:
                print('tool_input', tool_input)
            try:
                # Remove control characters
                tool_input = re.sub(r'[\x00-\x1F\x7F]', '', tool_input)

                # Attempt to load as JSON
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError as e:
                self.errors_encountered.append(e)
                print(f"Error parsing tool input: {e}. JSON: {tool_input}")
                if self.debug:
                    raise ValueError(f"Error parsing JSON: {e}")
            try:
                tool_result = self.tool_by_names[tool].run(**tool_input)
                if self.verbose:
                    print('tool_result', tool_result)
            except Exception as e:
                self.errors_encountered.append(e)
                if self.debug:
                    raise ValueError(f"Error from tool: {e}")
                # if not debug, add this as the observation so the agent can try again
                tool_result = f"Error from tool: {e}"
            generated += f"\n{OBSERVATION_TOKEN} {tool_result}\nNext Thought:"
            self.ai_responses.append(generated.strip())
            if self.verbose:
                print('------')
                print('PARSED GENERATED')
                print('------')
                print(generated)
                print('------')
            previous_responses.append(generated)
            if tool == 'Return Final Answer Tool':
                if self.verbose:
                    print('------')
                    print('FINAL PROMPT')
                    print('------')
                    print(curr_prompt)
                    print('------')
                task.raw_output = tool_result
                task.completed = True
                return tool_result

    def decide_next_action(self, prompt: str) -> str:
        generated = self.llm.generate(
            [{'role': 'user', 'content': prompt}],
            stop=self.stop_pattern)

        tool, tool_input = self._parse(generated)
        if self.verbose:
            print('tool', tool)
            print('tool_input', tool_input)
        return generated, tool, tool_input

    def _parse(self, generated: str) -> Tuple[str, str]:
        # if 'Return Final Answer Tool' in generated:
        #     final_answer = generated.split('Action Input:')[-1].strip()
        #     return "Return Final Answer Tool", final_answer
        regex = r"Action: [\[]?(.*?)[\]]?[\n]*Action Input:[\s]*(.*)"
        match = re.search(regex, generated, re.DOTALL)
        if not match:
            self.errors_encountered.append(
                ValueError(f"Output of LLM is not parsable for next tool use: `{generated}`"))
            if self.debug:
                raise ValueError(f"Output of LLM is not parsable for next tool use: `{generated}`")
            # if not debug, add this as the observation so the agent can try again
            tool = F'TOOL ERROR. MAKE SURE TO VERBTAIM STATE A TOOL NAME FROM THE LIST: {self.tool_names}'
            tool_input = tool
        tool = match.group(1).strip()
        tool_input = match.group(2)
        return tool, tool_input.strip(" ").strip('"')
