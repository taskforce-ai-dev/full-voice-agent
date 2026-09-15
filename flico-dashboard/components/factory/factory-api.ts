export type FactoryAction =
  | "intake"
  | "capabilities"
  | "sources"
  | "plan/confirm"
  | "handoff";

type FactoryActionPayload = Record<string, unknown>;

function readCsrfToken() {
  if (typeof document === "undefined") return "";
  const token = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("factory_csrf="));
  return token ? decodeURIComponent(token.slice("factory_csrf=".length)) : "";
}

/**
 * All mutations intentionally stay same-origin. The private admin service can
 * attach its CSRF cookie and implement these routes without exposing provider
 * credentials to the browser.
 */
export async function postFactoryAction(
  action: FactoryAction,
  payload: FactoryActionPayload,
) {
  const response = await fetch(`/api/factory/${action}`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readCsrfToken(),
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Factory action failed (${response.status})`);
  }

  return response.json().catch(() => ({}));
}
