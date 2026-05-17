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

async function loadChatPanel(scopeType, scopeId) {
  const target = document.querySelector(".chat-panel");
  if (!target || !scopeType || !scopeId) {
    return;
  }

  const response = await fetch(`/chat/${scopeType}/${scopeId}`, {
    headers: {
      "HX-Request": "true",
    },
  });
  if (!response.ok) {
    return;
  }

  target.outerHTML = await response.text();
}

function createChatMessage(role, text) {
  const wrapper = document.createElement("div");
  wrapper.className = `chat-message chat-message--${role}`;

  const label = document.createElement("span");
  label.textContent = role === "user" ? "Zach" : "Jo";

  const body = document.createElement("p");
  body.textContent = text;

  wrapper.append(label, body);
  return wrapper;
}

function parseEventPayload(event) {
  try {
    return JSON.parse(event.data);
  } catch (_error) {
    return {};
  }
}

async function submitChatForm(form) {
  const textarea = form.querySelector('textarea[name="message"]');
  const button = form.querySelector('button[type="submit"]');
  const stack = document.querySelector(".message-stack");
  const message = textarea ? textarea.value.trim() : "";

  if (!message || !stack) {
    return;
  }

  const formData = new FormData(form);
  stack.appendChild(createChatMessage("user", message));
  const assistantMessage = createChatMessage("assistant", "");
  const assistantBody = assistantMessage.querySelector("p");
  stack.appendChild(assistantMessage);
  stack.scrollTop = stack.scrollHeight;

  if (textarea) {
    textarea.value = "";
    textarea.disabled = true;
  }
  if (button) {
    button.disabled = true;
  }

  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: formData,
    });
    if (!response.ok) {
      throw new Error("Message failed");
    }

    const payload = await response.json();
    const stream = new EventSource(`/chat/streams/${payload.stream_id}`);

    stream.addEventListener("delta", (event) => {
      const data = parseEventPayload(event);
      assistantBody.textContent += data.text || "";
      stack.scrollTop = stack.scrollHeight;
    });

    stream.addEventListener("done", () => {
      stream.close();
      if (textarea) {
        textarea.disabled = false;
        textarea.focus();
      }
      if (button) {
        button.disabled = false;
      }
    });

    stream.addEventListener("error", (event) => {
      const data = event.data ? parseEventPayload(event) : {};
      if (data.message) {
        assistantBody.textContent = data.message;
      }
      stream.close();
      if (textarea) {
        textarea.disabled = false;
      }
      if (button) {
        button.disabled = false;
      }
    });
  } catch (error) {
    assistantBody.textContent = error.message;
    if (textarea) {
      textarea.disabled = false;
    }
    if (button) {
      button.disabled = false;
    }
  }
}

document.addEventListener("submit", (event) => {
  const chatForm = event.target.closest("form[data-chat-form]");
  if (chatForm) {
    event.preventDefault();
    submitChatForm(chatForm);
    return;
  }

  const form = event.target.closest("form[hx-post]");
  if (!form) {
    return;
  }
  event.preventDefault();
  sendHtmxRequest(form);
});

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("button[hx-post]");
  if (trigger) {
    event.preventDefault();
    sendHtmxRequest(trigger);
    return;
  }

  if (event.target.closest("button, form, a, textarea, input")) {
    return;
  }

  const chatTarget = event.target.closest("[data-chat-scope-type][data-chat-scope-id]");
  if (!chatTarget) {
    return;
  }

  loadChatPanel(
    chatTarget.getAttribute("data-chat-scope-type"),
    chatTarget.getAttribute("data-chat-scope-id"),
  );
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }

  const chatTarget = event.target.closest("[data-chat-scope-type][data-chat-scope-id]");
  if (!chatTarget) {
    return;
  }

  event.preventDefault();
  loadChatPanel(
    chatTarget.getAttribute("data-chat-scope-type"),
    chatTarget.getAttribute("data-chat-scope-id"),
  );
});
