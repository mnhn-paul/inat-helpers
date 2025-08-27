---
layout: default
title: Tool Index
---

<div id="tool-container"></div>

<script>
const tools = [
  {name:"New Taxa Tool", file:"new-taxa-user.html", category:"User Tools", description:"Quickly check if a user observed species for the first time within a given time period."},
  {name:"ZPIN Tool", file:"ZPIN-user.html", category:"User Tools", description:"Identify how many observations you contributed from the ZPIN protected area in Luxembourg."},
  {name:"User TOD slider", file:"daily-user-map-timeline.html", category:"User Tools", description:"Check for possible issues in location data over time."},
  {name:"Top users in Luxembourg", file:"top-users-lux.html", category:"User Tools", description:"List of top contributors in Luxembourg."},
  {name:"Compare user species lists", file:"compare_species_user.html", category:"User Tools", description:"Compare the species lists of two users."},
  {name:"Yearly species observation histogram", file:"species-yearly-histogram.html", category:"Luxembourg Data", description:"Lookup a species name to generate yearly histograms of observations."},
  {name:"Yearly observation histogram Luxembourg", file:"inat-Lux-yearly-evolution.html", category:"Luxembourg Data", description:"Explore yearly evolution of Luxembourg observations on iNaturalist."},
  {name:"CNC 2025 user overview", file:"CNC2025-users.html", category:"City Nature Challenge", description:"Overview of CNC 2025 user activity."},
  {name:"CNC quick data overview", file:"CNC-data-overview.html", category:"City Nature Challenge", description:"Quick glance at CNC biodiversity data."},
  {name:"Places", file:"places.html", category:"Other", description:"Testing tool for locations."}
];

const specialLinks = [
  {name:"Useful links", file:"pages/links.html", description:"Collection of useful biodiversity resources."}
];

// Group tools by category
const grouped = tools.reduce((acc, tool) => {
  if (!acc[tool.category]) acc[tool.category] = [];
  acc[tool.category].push(tool);
  return acc;
}, {});

const container = document.getElementById("tool-container");

for (const category in grouped) {
  const section = document.createElement("div");
  section.className = "tool-category";
  const heading = document.createElement("h2");
  heading.textContent = category;
  section.appendChild(heading);

  grouped[category].forEach(tool => {
    const toolDiv = document.createElement("div");
    toolDiv.className = "tool";
    const link = document.createElement("a");
    link.href = `Tools/${tool.file}`;
    link.textContent = tool.name;
    const desc = document.createElement("div");
    desc.className = "description";
    desc.textContent = tool.description;
    toolDiv.appendChild(link);
    toolDiv.appendChild(desc);
    section.appendChild(toolDiv);
  });

  container.appendChild(section);
}

// Special Links
const linksSection = document.createElement("div");
linksSection.className = "tool-category special-links";
const linksHeading = document.createElement("h2");
linksHeading.textContent = "Useful Resources";
linksSection.appendChild(linksHeading);

specialLinks.forEach(linkObj => {
  const linkDiv = document.createElement("div");
  linkDiv.className = "tool special";
  const link = document.createElement("a");
  link.href = linkObj.file;
  link.textContent = linkObj.name;
  const desc = document.createElement("div");
  desc.className = "description";
  desc.textContent = linkObj.description;
  linkDiv.appendChild(link);
  linkDiv.appendChild(desc);
  linksSection.appendChild(linkDiv);
});

container.appendChild(linksSection);
</script>
