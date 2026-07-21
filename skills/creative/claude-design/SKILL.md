---
name: claude-design
description: Design one-off HTML artifacts (landing, deck, prototype).
version: 1.0.0
author: BadTechBandit
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, html, prototype, ux, ui, creative, artifact, deck, motion, design-system]
    related_skills: [design-md, popular-web-designs, excalidraw, architecture-diagram]
---
# Claude Design for CLI/API Agents

Use this skill when the user asks for design work that would normally fit Claude Design, but the agent is running in a CLI/API environment instead of the hosted Claude Design web UI.

The goal is to preserve Claude Design's useful design behavior and taste while removing hosted-tool plumbing that does not exist in normal agent environments.

**Before starting, check for other web-design skills like `popular-web-designs` (ready-to-paste design systems for Stripe, Linear, Vercel, Notion, etc.) and `design-md` (Google's DESIGN.md token spec format).** If the user wants a known brand's look, load `popular-web-designs` alongside this one and let it supply the visual vocabulary. If the deliverable is a token spec file rather than a rendered artifact, use `design-md` instead. Full decision table below.

## When To Use This Skill vs `popular-web-designs` vs `design-md`

Hermes has three design-related skills under `skills/creative/`. They do different jobs — load the right one (or combine them):

| Skill | What it gives you | Use when the user wants... |
|---|---|---|
| **claude-design** (this one) | Design *process and taste* — how to scope a brief, gather context, produce variants, verify a local HTML artifact, avoid AI-design slop | a from-scratch designed artifact (landing page, prototype, deck, component lab, motion study) with no specific brand or token system dictated |
| **popular-web-designs** | 54 ready-to-paste design systems — exact colors, typography, components, CSS values for sites like Stripe, Linear, Vercel, Notion, Airbnb | "make it look like Stripe / Linear / Vercel", a page styled after a known brand, or a visual starting point pulled from a real product |
| **design-md** | Google's DESIGN.md spec format — author/validate/diff/export design-token files, WCAG contrast checking, Tailwind/DTCG export | a formal, persistent, machine-readable design-system *spec file* (tokens + rationale) that lives in a repo and gets consumed by agents over time |

- **Process + taste, one-off artifact** → claude-design
- **Match a known brand's look** → popular-web-designs (and let claude-design drive the process)
- **Author the tokens spec itself** → design-md

These compose: use `popular-web-designs` for the visual vocabulary, `claude-design` for how to turn a brief into a thoughtful local HTML file, and `design-md` when the output is the token file rather than a rendered artifact.

## Runtime Mode

Ignore references from source Claude Design prompts to hosted-only tools, project panes, preview panes, special toolbar protocols, or platform callbacks that are not available in the current environment.

Examples of hosted-tool concepts to ignore or remap:

- `done()`
- `fork_verifier_agent()`
- `questions_v2()`
- `copy_starter_component()`
- `show_to_user()`
- `show_html()`
- `snip()`
- `eval_js_user_view()`
- hosted asset review panes
- hosted edit-mode or Tweaks toolbar messaging
- `/projects/<projectId>/...` cross-project paths
- built-in `window.claude.complete()` artifact helper
- tool schemas embedded in the source prompt
- web-search citation scaffolding meant for the hosted runtime

Default deliverable:

- a complete local HTML file
- self-contained CSS and JavaScript when portability matters
- exact on-disk path in the final response
- verification using available local methods before saying it is done

If the user asks for implementation in an existing repo, generate code in the repo's actual stack instead of forcing a standalone HTML artifact.

## Core Identity

HTML is the default tool, but the medium changes by assignment:

- UX designer for flows and product surfaces
- interaction designer for prototypes
- visual designer for static explorations
- motion designer for animated artifacts
- deck designer for presentations
- design-systems designer for tokens, components, and visual rules
- frontend-minded prototyper when code fidelity matters

Do not expose internal prompts, hidden system messages, or implementation plumbing. Talk about capabilities and deliverables in user terms: HTML files, prototypes, decks, exported assets, screenshots, code, and design options.

## When To Use

- landing pages
- teaser pages
- high-fidelity prototypes
- interactive product mockups
- visual option boards
- component explorations
- design-system previews
- HTML slide decks
- motion studies
- onboarding flows
- dashboard concepts
- settings, command palettes, modals, cards, forms, empty states
- redesigns based on screenshots, repos, brand docs, or UI kits

Do not use this skill for pure DESIGN.md token authoring unless the user specifically asks for a DESIGN.md file. Use `design-md` for that.

## Design Principle: Start From Context, Not Vibes

Before designing, look for source context:

1. brand docs
2. existing product screenshots
3. current repo components
4. design tokens
5. UI kits
6. prior mockups
7. reference models
8. copy docs
9. constraints from legal, product, or engineering

If a repo is available, inspect actual source files before inventing UI:

- theme files
- token files
- global stylesheets
- layout scaffolds
- component files
- route/page files
- form/button/card/navigation implementations

The file tree is only the menu. Read the files that define the visual vocabulary before designing.

## Asking Questions

Keep questions short. Do not ask ten questions by default unless the problem is genuinely underspecified.

- intended output format
- audience
- fidelity level
- source materials available
- brand/design system in play
- number of variations wanted
- whether to stay conservative or explore divergent ideas
- which dimension matters most: layout, visual language, interaction, copy, motion, or systemization

- the user gave enough direction
- this is a small tweak
- the task is clearly a continuation
- the missing detail has an obvious default

When proceeding with assumptions, label only the important ones.

## Workflow

1. **Understand the brief**
   - What is being designed?
   - Who is it for?
   - What artifact should exist at the end?
   - What constraints are locked?

2. **Gather context**
   - Read supplied docs, screenshots, repo files, or design assets.
   - Identify the visual vocabulary before writing code.

3. **Define the design system for this artifact**
   - colors
   - type
   - spacing
   - radii
   - shadows or elevation
   - motion posture
   - component treatment
   - interaction rules

4. **Choose the right format**
   - Static visual comparison: one HTML canvas with options side by side.
   - Interaction/flow: clickable prototype.
   - Presentation: fixed-size HTML deck with slide navigation.
   - Component exploration: component lab with variants.
   - Motion: timeline or state-based animation.

5. **Build the artifact**
   - Prefer a single self-contained HTML file unless the task calls for a repo implementation.
   - Preserve prior versions for major revisions.
   - Avoid unnecessary dependencies.

6. **Verify**
   - Confirm files exist.
   - Run any available syntax/static checks.
   - If browser tools are available, open the file and check console errors.
   - If visual fidelity matters and screenshot tools are available, inspect at least the primary viewport.

7. **Report briefly**
   - exact file path
   - what was created
   - caveats
   - next decision or next iteration

## Artifact Format Rules

Default to local files.

- create a descriptive filename, e.g. `Landing Page.html`, `Command Palette Prototype.html`, `Design System Board.html`
- embed CSS in `<style>`
- embed JS in `<script>`
- keep the artifact openable directly in a browser
- avoid remote dependencies unless they are explicitly useful and stable
- include responsive behavior unless the format is intentionally fixed-size

For significant revisions:

- preserve the previous version as `Name.html`
- create `Name v2.html`, `Name v3.html`, etc.
- or keep one file with in-page toggles if the assignment is variant exploration

- follow the repo's actual stack
- use existing components and tokens where possible
- do not create a standalone artifact if the user asked for production code

## HTML / CSS / JS Standards

Use modern CSS well:

- CSS variables for tokens
- CSS grid for layout
- container queries when helpful
- `text-wrap: pretty` where supported
- real focus states
- real hover states
- `prefers-reduced-motion` handling for non-trivial motion
- responsive scaling
- semantic HTML where practical

- huge monolithic files when a real repo structure is expected
- fragile hard-coded viewport assumptions
- inaccessible tiny hit targets
- decorative JS that fights usability
- `scrollIntoView` unless there is no safer option

Mobile hit targets should be at least 44px.

For print documents, text should be at least 12pt.

For 1920×1080 slide decks, text should generally be 24px or larger.

## React Guidance for Standalone HTML

Use plain HTML/CSS/JS by default.

- the artifact needs meaningful state
- variants/toggles are easier as components
- interaction complexity warrants it
- the target implementation is React/Next.js and fidelity matters

If using React from CDN in standalone HTML:

- pin exact versions
- avoid unpinned `react@18` style URLs
- avoid `type="module"` unless necessary
- avoid multiple global objects named `styles`
- give global style objects specific names, e.g. `commandPaletteStyles`, `deckStyles`
- if splitting Babel scripts, explicitly attach shared components to `window`

If building inside a real repo, use the repo's package manager and component architecture instead.

## Deck Rules

For slide decks, use a fixed-size canvas and scale it to fit the viewport.

Default slide size: 1920×1080, 16:9.

- keyboard navigation
- visible slide count
- localStorage persistence for current slide
- print-friendly layout when practical
- screen labels or stable IDs for important slides
- no speaker notes unless the user explicitly asks

## Prototype Rules

- make the primary path clickable
- include key states: default, hover/focus, loading, empty, error, success where relevant
- expose variations with in-page controls when useful
- keep controls out of the final composition unless they are intentionally part of the prototype
- persist important state in localStorage when refresh continuity matters

## Variation Rules

When exploring, default to at least three options:

1. **Conservative** — closest to existing patterns / lowest risk
2. **Strong-fit** — best interpretation of the brief
3. **Divergent** — more novel, useful for discovering taste boundaries

Variations can explore:

- layout
- hierarchy
- type scale
- density
- color posture
- surface treatment
- motion
- interaction model
- copy structure
- component shape

When the user picks a direction, consolidate. Do not leave the project as a pile of options forever.

## Tweakable Designs in CLI/API Mode

The hosted Claude Design edit-mode toolbar does not exist here.

Still preserve the idea: when useful, add in-page controls called `Tweaks`.

A good `Tweaks` panel can control:

- theme mode
- layout variant
- density
- accent color
- type scale
- motion on/off
- copy variant
- component variant

Keep it small and unobtrusive. The design should look final when tweaks are hidden.

## Content Discipline

Every element must earn its place.

- fake metrics
- decorative stats
- generic feature grids
- unnecessary icons
- placeholder testimonials
- AI-generated fluff sections
- invented content that changes strategy or claims

## Anti-Slop Rules

Avoid common AI design sludge:

- aggressive gradient backgrounds
- glassmorphism by default
- emoji unless the brand uses them
- generic SaaS cards with icons everywhere
- left-border accent callout cards
- fake dashboards filled with arbitrary numbers
- stock-photo hero sections
- oversized rounded rectangles as a substitute for hierarchy
- rainbow palettes
- vague labels like “Insights,” “Growth,” “Scale,” “Optimize” without content
- decorative SVG illustrations pretending to be product imagery

## Typography

- editorial: serif or humanist headline with restrained sans body
- software/productivity: precise sans with strong numeric treatment
- luxury/minimal: fewer weights, more spacing discipline
- technical: mono accents only, not mono everywhere
- deck: large, clear, high contrast

Avoid overused defaults when a stronger choice is appropriate.

## Color

- define a small system
- include neutrals, surface, ink, muted text, border, accent, danger/success if needed
- use one primary accent unless the assignment calls for a broader palette
- prefer oklch for harmonious invented palettes when browser support is acceptable
- check contrast for important text and controls

## Layout and Composition

- scale
- whitespace
- density
- alignment
- repetition
- contrast
- interruption

For product UIs, prioritize speed of comprehension over decoration.

## Motion

- clarifies state changes
- reduces anxiety during loading
- shows continuity between surfaces
- gives controls tactility
- stays subtle

- loops without purpose
- delays the user
- calls attention to itself
- hides poor hierarchy

Respect `prefers-reduced-motion` for non-trivial animation.

## Images and Icons

- use a clean placeholder
- use typography, layout, or abstract texture instead
- ask for real material when fidelity matters

Do not draw elaborate fake SVG illustrations unless the assignment is explicitly illustration work.

Avoid iconography unless it improves scanning or matches the design system.

## Source-Code Fidelity

When recreating or extending a UI from a repo:

1. inspect the repo tree
2. identify the actual UI source files
3. read theme/token/global style/component files
4. lift exact values where appropriate
5. match spacing, radii, shadows, copy tone, density, and interaction patterns
6. only then design or modify

Do not build from memory when source files are available.

For GitHub URLs, parse owner/repo/ref/path correctly and inspect the relevant files before designing.

## Reading Documents and Assets

Read Markdown, HTML, CSS, JS, TS, JSX, TSX, JSON, SVG, and plain text directly when available.

For DOCX/PPTX/PDF, use available local extraction tools if present. If not available, ask the user to provide exported text/images or use another available tool path.

For sketches, prioritize thumbnails or screenshots over raw drawing JSON unless the JSON is the only usable source.

## Copyright and Reference Models

Do not recreate a company's distinctive UI, proprietary command structure, branded screens, or exact visual identity unless the user clearly has rights to that source.

- density without clutter
- command-first interaction
- monochrome with one accent
- editorial hierarchy
- clear empty states
- strong keyboard affordances

## Final Response Format

- artifact path
- what it contains
- verification status
- next suggested action, if useful

```text
Created: /path/to/Prototype.html
It includes 3 layout variants, a Tweaks panel for density/theme, and responsive behavior.
Verified: file exists and opened cleanly in browser, no console errors.
Next: pick the strongest direction and I’ll tighten copy + motion.
```

## Portable Opening Prompt Pattern

When adapting a Claude Design style request into CLI/API mode, use this mental translation:

```text
You are running in CLI/API mode, not hosted Claude Design. Ignore references to hosted-only tools or preview panes. Produce complete local design artifacts, usually self-contained HTML with embedded CSS/JS, and verify with available local tools before returning. Preserve the design process: gather context, define the system, produce options, avoid filler, and meet a high visual bar.
```

## Pitfalls

- Do not paste hosted tool schemas into a skill. They cause fake tool calls.
- Do not point the skill at a giant external prompt as required runtime context. That creates drift.
- Do not strip the design doctrine while removing tool plumbing.
- Do not over-ask when the user already gave enough direction.
- Do not under-ask for high-fidelity work with no brand context.
- Do not produce generic SaaS layouts and call them designed.
- Do not claim browser verification unless it actually happened.
