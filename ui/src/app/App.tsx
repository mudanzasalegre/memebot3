import { RouterProvider } from "react-router-dom";

import { AuthProvider } from "../auth/AuthProvider";
import { DrawerProvider } from "./drawer";
import { router } from "./router";


export default function App() {
  return (
    <AuthProvider>
      <DrawerProvider>
        <RouterProvider router={router} />
      </DrawerProvider>
    </AuthProvider>
  );
}
