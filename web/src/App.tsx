import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, getHospital, getToken, setHospital, setToken } from "./api";
import { Logo } from "./components/ui";
import Bank from "./pages/Bank";
import CaseDetail from "./pages/CaseDetail";
import Cases from "./pages/Cases";
import Letters from "./pages/Letters";
import Login from "./pages/Login";
import NewCase from "./pages/NewCase";
import Owner from "./pages/Owner";
import Review from "./pages/Review";
import Setup from "./pages/Setup";
import Worklist from "./pages/Worklist";

export type Me = {
  user: { id: string; name: string; email: string; role: string; hospital_id: string | null };
  hospitals: { id: string; name: string; code: string }[];
  demo_mode: boolean;
};

const MeContext = createContext<Me | null>(null);
export const useMe = () => useContext(MeContext)!;

export const can = (me: Me, what: "desk" | "bank" | "clinical" | "admin" | "owner") => {
  const r = me.user.role;
  return {
    desk: ["desk", "owner", "hospital_admin", "aeternix_admin"].includes(r),
    bank: ["accountant", "owner", "hospital_admin", "aeternix_admin"].includes(r),
    clinical: ["clinician", "aeternix_admin"].includes(r),
    admin: ["hospital_admin", "aeternix_admin"].includes(r),
    owner: ["owner", "hospital_admin", "aeternix_admin", "accountant"].includes(r),
  }[what];
};

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(!!getToken());

  useEffect(() => {
    const out = () => setMe(null);
    window.addEventListener("aeternix:signed-out", out);
    if (getToken()) api.get<Me>("/api/auth/me").then(setMe).catch(() => setMe(null)).finally(() => setChecking(false));
    return () => window.removeEventListener("aeternix:signed-out", out);
  }, []);

  if (checking) return <div className="login"><p className="muted">Opening…</p></div>;
  if (!me) return <Login onSignedIn={setMe} />;

  if (!me.user.hospital_id && !getHospital()) {
    if (me.hospitals.length === 1) setHospital(me.hospitals[0].id);
    else return <ChooseHospital me={me} onChosen={() => setMe({ ...me })} />;
  }

  const hospital = me.hospitals.find((h) => h.id === (me.user.hospital_id || getHospital()));
  const signOut = () => { setToken(null); setHospital(null); setMe(null); };

  return (
    <MeContext.Provider value={me}>
      <div className="shell">
        <nav className="rail" aria-label="Main">
          <div className="brand"><Logo /><small>{hospital?.name}</small></div>
          {(can(me, "desk") || me.user.role === "clinician") && <NavLink to="/" end>Worklist</NavLink>}
          {can(me, "desk") && <NavLink to="/cases/new">New case</NavLink>}
          <NavLink to="/cases">Cases</NavLink>
          {can(me, "bank") && <NavLink to="/bank">Bank</NavLink>}
          {(can(me, "clinical") || me.user.role === "owner") && <NavLink to="/review">Clinical review</NavLink>}
          {can(me, "desk") && <NavLink to="/letters">Letters</NavLink>}
          {can(me, "owner") && <NavLink to="/owner">Owner view</NavLink>}
          {(can(me, "admin") || me.user.role === "owner") && <NavLink to="/setup">Setup</NavLink>}
          <div className="spacer" />
          <div className="who">
            {me.user.name}<br />
            <button className="link" onClick={signOut}>Sign out</button>
            {!me.user.hospital_id && me.hospitals.length > 1 &&
              <> <button className="link" onClick={() => { setHospital(null); setMe({ ...me }); }}>Switch hospital</button></>}
          </div>
        </nav>
        <main>
          <Routes>
            <Route path="/" element={me.user.role === "accountant" ? <Navigate to="/bank" replace /> :
              me.user.role === "clinician" ? <Navigate to="/review" replace /> : <Worklist />} />
            <Route path="/cases" element={<Cases />} />
            <Route path="/cases/new" element={<NewCase />} />
            <Route path="/cases/:id" element={<CaseDetail />} />
            <Route path="/bank" element={<Bank />} />
            <Route path="/review" element={<Review />} />
            <Route path="/letters" element={<Letters />} />
            <Route path="/owner" element={<Owner />} />
            <Route path="/setup" element={<Setup />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </MeContext.Provider>
  );
}

function ChooseHospital({ me, onChosen }: { me: Me; onChosen: () => void }) {
  return (
    <div className="login">
      <div style={{ width: "min(420px, 100%)" }}>
        <div className="brand"><Logo height={24} /></div>
        <h2>Choose a hospital</h2>
        <p className="muted">Your account works across hospitals. Pick the one to open.</p>
        <div className="actions" style={{ marginTop: 14 }}>
          {me.hospitals.map((h) => (
            <button key={h.id} onClick={() => { setHospital(h.id); onChosen(); }}>{h.name}</button>
          ))}
        </div>
      </div>
    </div>
  );
}
