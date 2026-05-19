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

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || "";
}

function authAwareHeaders(extraHeaders = {}) {
  const headers = { ...extraHeaders };
  const token = csrfToken();
  if (token) {
    headers["X-CSRF-Token"] = token;
  }
  return headers;
}

function redirectToLoginIfNeeded(response) {
  if (!response.redirected) {
    return false;
  }
  try {
    const url = new URL(response.url);
    if (url.pathname === "/login") {
      window.location.assign(response.url);
      return true;
    }
  } catch (_error) {
    return false;
  }
  return false;
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
    headers: authAwareHeaders({
      "HX-Request": "true",
    }),
  });

  if (redirectToLoginIfNeeded(response)) {
    return;
  }
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
    headers: authAwareHeaders({
      "HX-Request": "true",
    }),
  });
  if (redirectToLoginIfNeeded(response)) {
    return;
  }
  if (!response.ok) {
    return;
  }

  target.outerHTML = await response.text();
  renderMarkdownMessages(document);
  setupChatHistory(document);
}

function filterSearchFromUrl(url) {
  const parsed = new URL(url, window.location.origin);
  return parsed.search;
}

async function applyDashboardFilter(url, pushState = true) {
  const search = filterSearchFromUrl(url);
  const [goalsResponse, stepsResponse] = await Promise.all([
    fetch(`/partials/goals${search}`, {
      headers: authAwareHeaders({
        "HX-Request": "true",
      }),
    }),
    fetch(`/partials/suggestions${search}`, {
      headers: authAwareHeaders({
        "HX-Request": "true",
      }),
    }),
  ]);

  if (redirectToLoginIfNeeded(goalsResponse) || redirectToLoginIfNeeded(stepsResponse)) {
    return;
  }
  if (!goalsResponse.ok || !stepsResponse.ok) {
    return;
  }

  const goalStrip = document.querySelector(".goal-strip");
  const stepsPanel = document.querySelector("#steps-panel");
  if (goalStrip) {
    goalStrip.outerHTML = await goalsResponse.text();
  }
  if (stepsPanel) {
    stepsPanel.outerHTML = await stepsResponse.text();
  }

  if (pushState) {
    window.history.pushState({}, "", `/${search}`);
  }
}

async function refreshStepsPanel() {
  const target = document.querySelector("#steps-panel");
  if (!target) {
    return;
  }

  const response = await fetch(`/partials/suggestions${window.location.search}`, {
    headers: authAwareHeaders({
      "HX-Request": "true",
    }),
  });
  if (redirectToLoginIfNeeded(response)) {
    return;
  }
  if (!response.ok) {
    return;
  }
  target.outerHTML = await response.text();
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderInlineMarkdown(value) {
  let html = escapeHtml(value);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return html;
}

function renderMarkdown(markdown) {
  const lines = markdown.replaceAll("\r\n", "\n").split("\n");
  const blocks = [];
  let paragraph = [];
  let listItems = [];
  let inFence = false;
  let codeLines = [];

  function flushParagraph() {
    if (!paragraph.length) {
      return;
    }
    blocks.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  }

  function flushList() {
    if (!listItems.length) {
      return;
    }
    blocks.push(`<ul>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
    listItems = [];
  }

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inFence) {
        blocks.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        inFence = false;
      } else {
        flushParagraph();
        flushList();
        inFence = true;
      }
      continue;
    }

    if (inFence) {
      codeLines.push(line);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length + 2;
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2].trim())}</h${level}>`);
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      listItems.push(bullet[1].trim());
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  if (inFence) {
    blocks.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  }
  flushParagraph();
  flushList();

  return blocks.join("");
}

function renderMarkdownInto(message, markdown) {
  message.dataset.markdownContent = markdown;
  const body = message.querySelector(".markdown-body");
  if (body) {
    body.innerHTML = renderMarkdown(markdown);
  }
}

function renderMarkdownMessages(root) {
  root.querySelectorAll(".chat-message[data-markdown-content]").forEach((message) => {
    renderMarkdownInto(message, message.dataset.markdownContent || "");
  });
}

function createChatMessage(role, text = "", id = null) {
  const wrapper = document.createElement("div");
  wrapper.className = `chat-message chat-message--${role}`;
  wrapper.dataset.markdownContent = text;
  if (id !== null && id !== undefined) {
    wrapper.dataset.messageId = id;
  }

  const label = document.createElement("span");
  label.textContent = role === "user" ? "Zach" : "Jo";

  const body = document.createElement("div");
  body.className = "markdown-body";

  wrapper.append(label, body);
  renderMarkdownInto(wrapper, text);
  return wrapper;
}

function distanceFromBottom(stack) {
  return stack.scrollHeight - stack.scrollTop - stack.clientHeight;
}

function isNearBottom(stack, threshold = 48) {
  return distanceFromBottom(stack) <= threshold;
}

function scrollToBottom(stack) {
  stack.scrollTop = stack.scrollHeight;
}

function oldestMessageId(stack) {
  const oldest = stack.querySelector(".chat-message[data-message-id]");
  return oldest ? oldest.dataset.messageId : stack.dataset.oldestMessageId;
}

async function loadOlderMessages(stack) {
  if (stack.dataset.loadingOlderMessages === "true") {
    return;
  }
  if (stack.dataset.hasMoreMessages !== "true") {
    return;
  }

  const scopeType = stack.dataset.chatScopeType;
  const scopeId = stack.dataset.chatScopeId;
  const beforeId = oldestMessageId(stack);
  if (!scopeType || !scopeId || !beforeId) {
    return;
  }

  stack.dataset.loadingOlderMessages = "true";
  const previousHeight = stack.scrollHeight;

  try {
    const response = await fetch(
      `/chat/${scopeType}/${scopeId}/messages?before_id=${beforeId}&limit=20`,
      { headers: authAwareHeaders() },
    );
    if (redirectToLoginIfNeeded(response)) {
      return;
    }
    if (!response.ok) {
      return;
    }

    const payload = await response.json();
    const messages = payload.messages || [];
    const firstChild = stack.firstChild;
    for (const message of messages) {
      stack.insertBefore(
        createChatMessage(message.role, message.content, message.id),
        firstChild,
      );
    }

    stack.dataset.hasMoreMessages = payload.has_more ? "true" : "false";
    if (messages.length) {
      stack.dataset.oldestMessageId = messages[0].id;
      stack.scrollTop += stack.scrollHeight - previousHeight;
    }
  } finally {
    stack.dataset.loadingOlderMessages = "false";
  }
}

function setupChatHistory(root) {
  root.querySelectorAll(".message-stack[data-chat-history]").forEach((stack) => {
    if (stack.dataset.chatHistoryInitialized === "true") {
      return;
    }
    stack.dataset.chatHistoryInitialized = "true";
    scrollToBottom(stack);
    stack.addEventListener("scroll", () => {
      if (stack.scrollTop <= 24) {
        loadOlderMessages(stack);
      }
    });
  });
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
  const userMessage = createChatMessage("user", message);
  stack.appendChild(userMessage);
  const assistantMessage = createChatMessage("assistant", "");
  stack.appendChild(assistantMessage);
  scrollToBottom(stack);

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
      headers: authAwareHeaders(),
    });
    if (redirectToLoginIfNeeded(response)) {
      return;
    }
    if (!response.ok) {
      throw new Error("Message failed");
    }

    const payload = await response.json();
    if (payload.message_id) {
      userMessage.dataset.messageId = payload.message_id;
      if (!stack.dataset.oldestMessageId) {
        stack.dataset.oldestMessageId = payload.message_id;
      }
    }
    const stream = new EventSource(`/chat/streams/${payload.stream_id}`);
    let assistantMarkdown = "";

    stream.addEventListener("delta", (event) => {
      const shouldStickToBottom = isNearBottom(stack);
      const data = parseEventPayload(event);
      assistantMarkdown += data.text || "";
      renderMarkdownInto(assistantMessage, assistantMarkdown);
      if (shouldStickToBottom) {
        scrollToBottom(stack);
      }
    });

    stream.addEventListener("done", (event) => {
      const data = parseEventPayload(event);
      if (data.message_id) {
        assistantMessage.dataset.messageId = data.message_id;
      }
      stream.close();
      if (textarea) {
        textarea.disabled = false;
        textarea.focus();
      }
      if (button) {
        button.disabled = false;
      }
    });

    stream.addEventListener("receipt", (event) => {
      const data = parseEventPayload(event);
      if (data.text) {
        const shouldStickToBottom = isNearBottom(stack);
        const receiptMessage = createChatMessage("assistant", data.text, data.message_id);
        stack.appendChild(receiptMessage);
        if (shouldStickToBottom) {
          scrollToBottom(stack);
        }
      }
      refreshStepsPanel();
    });

    stream.addEventListener("error", (event) => {
      const data = event.data ? parseEventPayload(event) : {};
      if (data.message) {
        renderMarkdownInto(assistantMessage, data.message);
      }
      if (!assistantMarkdown) {
        window.location.assign("/login");
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
    renderMarkdownInto(assistantMessage, error.message);
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

  const filterLink = event.target.closest("a[data-dashboard-filter-url]");
  if (filterLink) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    applyDashboardFilter(filterLink.getAttribute("data-dashboard-filter-url"));
    return;
  }

  const explicitChatButton = event.target.closest("button[data-chat-scope-type][data-chat-scope-id]");
  if (explicitChatButton) {
    event.preventDefault();
    loadChatPanel(
      explicitChatButton.getAttribute("data-chat-scope-type"),
      explicitChatButton.getAttribute("data-chat-scope-id"),
    );
    return;
  }

  if (event.target.closest("button, form, a, textarea, input")) {
    return;
  }

  const chatTarget = event.target.closest("[data-chat-scope-type][data-chat-scope-id]");
  if (!chatTarget) {
    return;
  }

  const filterUrl = chatTarget.getAttribute("data-filter-url");
  if (filterUrl) {
    applyDashboardFilter(filterUrl);
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

  if (event.target.closest("button, form, a, textarea, input")) {
    return;
  }

  const chatTarget = event.target.closest("[data-chat-scope-type][data-chat-scope-id]");
  if (!chatTarget) {
    return;
  }

  event.preventDefault();
  const filterUrl = chatTarget.getAttribute("data-filter-url");
  if (filterUrl) {
    applyDashboardFilter(filterUrl);
  }
  loadChatPanel(
    chatTarget.getAttribute("data-chat-scope-type"),
    chatTarget.getAttribute("data-chat-scope-id"),
  );
});

window.addEventListener("popstate", () => {
  applyDashboardFilter(window.location.href, false);
});

document.addEventListener("DOMContentLoaded", () => {
  renderMarkdownMessages(document);
  setupChatHistory(document);
});
