function parsePhrases(element) {
  try {
    const phrases = JSON.parse(element.dataset.phrases || "[]");
    return Array.isArray(phrases) ? phrases.filter((phrase) => phrase.trim()) : [];
  } catch {
    return [];
  }
}

function rotateTitle() {
  const title = document.querySelector("[data-title-rotator]");
  if (!title) {
    return;
  }

  const phrases = parsePhrases(title);
  if (phrases.length < 2) {
    return;
  }

  let index = 0;
  window.setInterval(() => {
    index = (index + 1) % phrases.length;
    title.textContent = phrases[index];
  }, 6000);
}

window.addEventListener("DOMContentLoaded", rotateTitle);
