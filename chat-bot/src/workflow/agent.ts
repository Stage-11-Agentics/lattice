import { generateObject } from "ai";
import { anthropic } from "@ai-sdk/anthropic";
import type { AgentLike } from "smithers-orchestrator";
import type { ZodObject } from "zod";
import { SYSTEM_PROMPT } from "./system-prompt";

/**
 * Custom AgentLike that wraps Anthropic's generateObject for structured output.
 * Smithers expects agents to implement the generate() method and return
 * a result with a `.text` property containing the output.
 */
export class LatticeInterpreterAgent implements AgentLike {
  id = "lattice-interpreter";

  constructor(private model: string) {}

  async generate(args: {
    prompt: string;
    abortSignal?: AbortSignal;
    outputSchema?: ZodObject<any>;
    timeout?: { totalMs: number };
  }): Promise<{ text: string; output?: any; _output?: any }> {
    const schema = args.outputSchema;
    if (!schema) {
      throw new Error("LatticeInterpreterAgent requires an output schema");
    }

    const { object } = await generateObject({
      model: anthropic(this.model),
      schema,
      system: SYSTEM_PROMPT,
      prompt: args.prompt,
      abortSignal: args.abortSignal,
    });

    // Smithers expects the output as a JSON string in `.text` and structured in `._output`
    return {
      text: JSON.stringify(object),
      _output: object,
      output: object,
    };
  }
}
