import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, clearToken, getToken } from "./api.js";
import { Sidebar, Topbar, Spinner } from "./ui.jsx";
import LoginPage from "./pages/Login.jsx";
import WorkbenchPage from "./pages/Workbench.jsx";
import DocumentDetailPage from "./pages/DocumentDetail.jsx";
import ReviewQueuePage from "./pages/ReviewQueue.jsx";
import RulebookPage from "./pages/Rulebook.jsx";
import AuditLogPage from "./pages/AuditLog.jsx";

function useHashRoute() {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

function Shell({ user, onLogout, queueCount }) {
  const hash = useHashRoute();
  const [notifications, setNotifications] = useState([]);
  const [unread, setUnread] = useState(0);
  const [panelOpen, setPanelOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  let content;
  if (hash.startsWith("#/documents/")) {
    content = <DocumentDetailPage documentId={hash.replace("#/documents/", "")} user={user} />;
  } else if (hash === "#/queue") {
    content = <ReviewQueuePage />;
  } else if (hash === "#/rulebook") {
    content = <RulebookPage user={user} />;
  } else if (hash === "#/audit") {
    content = <AuditLogPage />;
  } else {
    content = <WorkbenchPage />;
  }

  const refreshNotifications = useCallback(() => {
    api
      .notifications()
      .then((data) => {
        setNotifications(data.notifications);
        setUnread(data.unread);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshNotifications();
    const timer = setInterval(refreshNotifications, 15000);
    return () => clearInterval(timer);
  }, [refreshNotifications]);

  const handleItemClick = useCallback(
    async (item) => {
      if (!item.read) {
        try {
          await api.markNotificationRead(item.id);
        } catch {
          /* ignore */
        }
        refreshNotifications();
      }
      const target = item.payload?.document_id;
      if (target) {
        window.location.hash = `#/documents/${target}`;
        setPanelOpen(false);
      }
    },
    [refreshNotifications],
  );

  const handleMarkAllRead = useCallback(async () => {
    try {
      await api.markAllNotificationsRead();
    } catch {
      /* ignore */
    }
    refreshNotifications();
  }, [refreshNotifications]);

  return (
    <div className="min-h-screen bg-canvas">
      <Sidebar
        active={hash.startsWith("#/documents/") ? "#/" : hash.split("?")[0]}
        user={user}
        queueCount={queueCount}
        onLogout={onLogout}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />
      <Topbar
        notifications={notifications}
        unread={unread}
        panelOpen={panelOpen}
        onTogglePanel={() => setPanelOpen((v) => !v)}
        onMarkAllRead={handleMarkAllRead}
        onItemClick={handleItemClick}
        onOpenSidebar={() => setSidebarOpen(true)}
      />
      <main className="min-h-screen bg-canvas pt-14 lg:pl-[200px]">
        <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6">{content}</div>
      </main>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(null);
  const [booting, setBooting] = useState(getToken() !== "");
  const [queueCount, setQueueCount] = useState(0);

  useEffect(() => {
    if (!getToken()) {
      setBooting(false);
      return;
    }
    api
      .me()
      .then((data) => {
        if (data.user) setUser(data.user);
        else clearToken();
      })
      .catch(() => clearToken())
      .finally(() => setBooting(false));
  }, []);

  const refreshQueueCount = useCallback(() => {
    api
      .queue()
      .then((data) => setQueueCount(data.items.length))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!user) return;
    refreshQueueCount();
    const timer = setInterval(refreshQueueCount, 15000);
    return () => clearInterval(timer);
  }, [user, refreshQueueCount]);

  const handleLogout = () => {
    clearToken();
    setUser(null);
    window.location.hash = "#/login";
  };

  if (booting) {
    return (
      <div className="min-h-screen bg-canvas">
        <Spinner text="正在验证登录状态…" />
      </div>
    );
  }

  if (!user) {
    return (
      <LoginPage
        onLogin={(loggedInUser) => {
          setUser(loggedInUser);
          window.location.hash = "#/";
        }}
      />
    );
  }

  return <Shell user={user} onLogout={handleLogout} queueCount={queueCount} />;
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
