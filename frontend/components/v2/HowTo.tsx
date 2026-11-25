// "Show me how" with its steps: the disclosure every setup form and dialog uses for what a person does outside Graphico. The steps are copy
// (`canvas-copy.ts`), taken from the docs of each app.
import { ShowMeHow, StepList } from "@graphico/ui";
import { V2_COPY } from "./v2-copy";

export function HowTo({ steps }: Readonly<{ steps: readonly string[] }>) {
  return (
    <ShowMeHow label={V2_COPY.showMeHow}>
      <StepList>
        {steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </StepList>
    </ShowMeHow>
  );
}
