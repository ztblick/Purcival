function formDataForTrigger(trigger) {
  if (trigger instanceof HTMLFormElement) {
    return new FormData(trigger);
  }

  const formData = new FormData();
  if (trigger.name) {
    formData.append(trigger.name, trigger.value);
  }
  return formData;
}

async function sendHtmxRequest(trigger) {
  const url = trigger.getAttribute("hx-post");
  const targetSelector = trigger.getAttribute("hx-target");
  const swap = trigger.getAttribute("hx-swap") || "innerHTML";
  const target = targetSelector ? document.querySelector(targetSelector) : null;

  if (!url || !target) {
    return;
  }

  const response = await fetch(url, {
    method: "POST",
    body: formDataForTrigger(trigger),
    headers: {
      "HX-Request": "true",
    },
  });

  if (!response.ok) {
    return;
  }

  const html = await response.text();
  if (swap === "outerHTML") {
    target.outerHTML = html;
  } else {
    target.innerHTML = html;
  }
}

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[hx-post]");
  if (!form) {
    return;
  }
  event.preventDefault();
  sendHtmxRequest(form);
});

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("button[hx-post]");
  if (!trigger) {
    return;
  }
  event.preventDefault();
  sendHtmxRequest(trigger);
});
