import { createContext, startTransition, useContext, useState, type ReactNode } from "react";


export interface DrawerPanel {
  eyebrow?: string;
  title: string;
  description?: string;
  content: ReactNode;
}

interface DrawerContextValue {
  panel: DrawerPanel | null;
  openPanel: (panel: DrawerPanel) => void;
  closePanel: () => void;
}

const DrawerContext = createContext<DrawerContextValue | null>(null);


export function DrawerProvider({ children }: { children: ReactNode }) {
  const [panel, setPanel] = useState<DrawerPanel | null>(null);

  const value: DrawerContextValue = {
    panel,
    openPanel(nextPanel) {
      startTransition(() => {
        setPanel(nextPanel);
      });
    },
    closePanel() {
      setPanel(null);
    },
  };

  return <DrawerContext.Provider value={value}>{children}</DrawerContext.Provider>;
}


export function useDrawer() {
  const context = useContext(DrawerContext);
  if (!context) {
    throw new Error("useDrawer must be used inside DrawerProvider");
  }
  return context;
}
