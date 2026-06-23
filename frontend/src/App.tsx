import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { useAuth } from "./auth/AuthContext";
import { HistoryPage } from "./pages/HistoryPage";
import { QuestionsPage } from "./pages/QuestionsPage";
import { RoomPage, RoomsListPage } from "./pages/RoomPage";
import { SignInPage } from "./pages/SignInPage";
import { SolvePage } from "./pages/SolvePage";
import { TestPage } from "./pages/TestPage";

function Protected({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  // Wait for the session restore before deciding. Redirecting during the
  // initial load would bounce a signed-in user to the login page on refresh.
  if (loading) return <div className="boot">Loading…</div>;
  if (!user) return <Navigate to="/signin" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/signin" element={<SignInPage />} />
      <Route
        element={
          <Protected>
            <Layout />
          </Protected>
        }
      >
        <Route index element={<Navigate to="/questions" replace />} />
        <Route path="/questions" element={<QuestionsPage />} />
        <Route path="/solve/:questionId" element={<SolvePage />} />
        <Route path="/test" element={<TestPage />} />
        <Route path="/rooms" element={<RoomsListPage />} />
        <Route path="/rooms/:roomId" element={<RoomPage />} />
        <Route path="/history" element={<HistoryPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/questions" replace />} />
    </Routes>
  );
}
