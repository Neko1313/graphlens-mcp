import type { SidebarsConfig } from "@docusaurus/plugin-content-docs";

const sidebars: SidebarsConfig = {
  docs: [
    "intro",
    {
      type: "category",
      label: "Getting started",
      items: ["getting-started", "commands"],
    },
    {
      type: "category",
      label: "Guides",
      items: ["navigation", "freshness"],
    },
    {
      type: "category",
      label: "Reference",
      items: ["agent-tools", "architecture"],
    },
  ],
};

export default sidebars;
