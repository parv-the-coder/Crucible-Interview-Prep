import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function Layout() {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();

  return (
    <div className="shell">
      <header className="topbar">
        <NavLink to="/" className="brand">
          Crucible
        </NavLink>
        <nav className="nav">
          <NavLink to="/questions">Practice</NavLink>
          <NavLink to="/test">Timed test</NavLink>
          <NavLink to="/rooms">Interview room</NavLink>
          <NavLink to="/history">History</NavLink>
        </nav>
        <div className="account">
          {user && (
            <>
              <span className="rating" title="Your current rating">
                {Math.round(user.rating)}
              </span>
              <span className="who">{user.display_name}</span>
              <button
                className="link"
                onClick={async () => {
                  await signOut();
                  navigate("/signin");
                }}
              >
                Sign out
              </button>
            </>
          )}
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
