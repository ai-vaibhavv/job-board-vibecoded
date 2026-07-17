import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import JobsPage from "./pages/JobsPage";
import { ProfilePage } from "./pages/ProfilePage";
import SettingsPage from "./pages/SettingsPage";

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<JobsPage />} />
        <Route path="/jobs/:id" element={<JobsPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
