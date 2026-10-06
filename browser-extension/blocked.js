const q = new URLSearchParams(location.search);
document.getElementById("site").textContent = q.get("site") || "This site";
const left = parseFloat(q.get("left"));
if (!isNaN(left)) document.getElementById("left").textContent = `Break in ${Math.ceil(left)} min.`;
if (q.get("label")) document.getElementById("label").textContent = `Working on: ${q.get("label")}`;
