import type * as Preset from "@docusaurus/preset-classic";
import type { Config } from "@docusaurus/types";
import { themes as prismThemes } from "prism-react-renderer";

const config: Config = {
  title: "graphlens-mcp",
  tagline: "A semantic code graph MCP server for coding agents",
  favicon: "img/favicon.svg",

  url: "https://neko1313.github.io",
  baseUrl: "/graphlens-mcp/",

  organizationName: "Neko1313",
  projectName: "graphlens-mcp",
  trailingSlash: false,

  onBrokenLinks: "throw",
  markdown: {
    hooks: {
      onBrokenMarkdownLinks: "warn",
    },
  },

  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },

  presets: [
    [
      "classic",
      {
        docs: {
          // Docs-only mode: the docs live at the site root.
          routeBasePath: "/",
          sidebarPath: "./sidebars.ts",
          editUrl:
            "https://github.com/Neko1313/graphlens-mcp/tree/main/website/",
        },
        blog: false,
        theme: {
          customCss: "./src/css/custom.css",
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    navbar: {
      title: "graphlens-mcp",
      items: [
        {
          type: "docSidebar",
          sidebarId: "docs",
          position: "left",
          label: "Docs",
        },
        {
          href: "https://pypi.org/project/graphlens-mcp/",
          label: "PyPI",
          position: "right",
        },
        {
          href: "https://github.com/Neko1313/graphlens-mcp",
          label: "GitHub",
          position: "right",
        },
      ],
    },
    footer: {
      style: "dark",
      links: [
        {
          title: "Docs",
          items: [
            { label: "Introduction", to: "/" },
            { label: "Getting started", to: "/getting-started" },
            { label: "Agent tools", to: "/agent-tools" },
          ],
        },
        {
          title: "More",
          items: [
            {
              label: "graphlens engine",
              href: "https://github.com/Neko1313/graphlens",
            },
            {
              label: "GitHub",
              href: "https://github.com/Neko1313/graphlens-mcp",
            },
          ],
        },
      ],
      copyright: `MIT-licensed. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ["bash", "toml", "python", "json"],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
