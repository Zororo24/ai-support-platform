import { useCallback, useState } from "react";

import { AgentsPage } from "./agents/AgentsPage";
import { ConnectionsPage } from "./admin/ConnectionsPage";
import { InsightsPage } from "./admin/InsightsPage";
import { KnowledgePage } from "./admin/KnowledgePage";
import { UsersPage } from "./admin/UsersPage";
import { ApprovalsPage } from "./admin/ApprovalsPage";
import { SettingsPage } from "./admin/SettingsPage";
import { WebhooksPage } from "./admin/WebhooksPage";
import { RunsPage } from "./runs/RunsPage";
import { useAuth } from "./auth/AuthContext";
import { LoginPage } from "./auth/LoginPage";
import { ChatPage } from "./chat/ChatPage";
import { TicketsPage } from "./tickets/TicketsPage";

type Tab =
  | "agents"
  | "chat"
  | "tickets"
  | "knowledge"
  | "connections"
  | "approvals"
  | "webhooks"
  | "runs"
  | "insights"
  | "settings"
  | "users";
const ADMIN_TABS: Tab[] = [
  "agents",
  "knowledge",
  "connections",
  "approvals",
  "webhooks",
  "runs",
  "insights",
  "settings",
  "users",
];

export function App() {
  const { user, loading, logout, logoutAll } = useAuth();
  const [tab, setTab] = useState<Tab | null>(null); // null = the role's home tab
  const [seedTitle, setSeedTitle] = useState<string | null>(null);
  const clearSeed = useCallback(() => setSeedTitle(null), []);

  if (loading) return <main className="shell">Loading…</main>;
  if (!user) {
    return (
      <main className="shell">
        <LoginPage />
      </main>
    );
  }

  const isAdmin = user.role === "admin";
  // A customer can never land on an admin tab, even if state was left over from an admin session.
  const requested: Tab = tab ?? (isAdmin ? "agents" : "chat");
  const active: Tab = ADMIN_TABS.includes(requested) && !isAdmin ? "chat" : requested;
  const wide = active !== "chat";

  const tabButton = (name: Tab, label: string) => (
    <button className={active === name ? "active" : ""} onClick={() => setTab(name)}>
      {label}
    </button>
  );

  return (
    <main className={`shell ${wide ? "wide" : ""}`}>
      <header>
        <h1>{user.tenant_name} Support</h1>
        <nav>
          {tabButton("chat", "Chat")}
          {tabButton("tickets", "Tickets")}
          {isAdmin && tabButton("agents", "Agents")}
          {isAdmin && tabButton("knowledge", "Knowledge base")}
          {isAdmin && tabButton("connections", "Connections")}
          {isAdmin && tabButton("approvals", "Approvals")}
          {isAdmin && tabButton("webhooks", "Webhooks")}
          {isAdmin && tabButton("runs", "Runs")}
          {isAdmin && tabButton("insights", "Insights")}
          {isAdmin && tabButton("settings", "Settings")}
          {isAdmin && tabButton("users", "Users")}
        </nav>
        <span className="who">
          {user.email} ({user.role})
          <button onClick={logout}>Log out</button>
          <button
            onClick={() => {
              if (window.confirm("Sign out on every device where you are logged in?")) void logoutAll();
            }}
            title="Ends all your sessions, including other browsers"
          >
            Sign out everywhere
          </button>
        </span>
      </header>
      {active === "agents" && <AgentsPage />}
      {active === "chat" && <ChatPage isAdmin={isAdmin} />}
      {active === "tickets" && <TicketsPage role={user.role} />}
      {active === "knowledge" && <KnowledgePage seedTitle={seedTitle} onSeedUsed={clearSeed} />}
      {active === "connections" && <ConnectionsPage />}
      {active === "approvals" && <ApprovalsPage />}
      {active === "webhooks" && <WebhooksPage />}
      {active === "settings" && <SettingsPage />}
      {active === "runs" && <RunsPage />}
      {active === "insights" && (
        <InsightsPage
          onAddDocument={(question) => {
            setSeedTitle(question);
            setTab("knowledge");
          }}
        />
      )}
      {active === "users" && <UsersPage currentUserId={user.id} />}
    </main>
  );
}
