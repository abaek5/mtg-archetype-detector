# MTG Archetype Detector

A live match companion tool for Magic: The Gathering. Identifies your opponent's deck archetype as you enter cards, with a stacking analysis feed and AI-powered gameplay advice.

## Features

- **Live feed** — analysis stacks as you add opponent cards, newest on top
- **Auto-analysis** — triggers on every card added, no button needed
- **Confidence timeline** — tracks how the read evolves across the match
- **35+ archetypes** across Modern, Legacy, Pioneer, Historic, Standard, and Commander
- **Pattern matching** — instant results from a built-in archetype database
- **Claude AI fallback** — identifies unknown or off-meta decks using the Anthropic API
- **Gameplay advisor** — tailored in-game tips based on your loaded deck vs the identified archetype
- **New game button** — wipes the feed clean between rounds

## Setup

1. Open `index.html` in any browser (no server needed locally)
2. Enter your [Anthropic API key](https://console.anthropic.com) for AI fallback + gameplay advice
3. Paste your Arena decklist (Arena → Share → Copy Decklist)
4. Select your format
5. Type opponent cards as they play them — analysis updates automatically

## Deployment

Hosted on Netlify via this repo. Any push to `main` triggers a redeploy.

## Adding Archetypes

Archetypes are defined in the `ALL_ARCHETYPES` array in `index.html`. Each entry follows this shape:

```js
{
  name: "Deck Name",
  formats: ["Modern", "Historic"],   // which formats it appears in
  colors: ["Blue", "Red"],           // color identity
  strategy: "Tempo",                 // archetype strategy label
  cards: ["card name", ...],         // lowercase card names for matching
  desc: "Two sentence description.", 
  counters: "How to beat it."
}
```

## Tech

- Vanilla HTML/CSS/JS — zero dependencies, zero build step
- [Tabler Icons](https://tabler-icons.io) for iconography
- [Anthropic API](https://docs.anthropic.com) for AI analysis and gameplay advice
# Reset fix
# reset fix
