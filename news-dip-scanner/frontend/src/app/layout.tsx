import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Dip scanner", template: "%s · Dip scanner" },
  description: "News-driven price dips, rated by language models. Invite only.",
  robots: { index: false, follow: false },
  // iOS doesn't use the SVG icon (app/icon.svg) on the home screen: public/apple-touch-icon.png, 180x180, opaque.
  // Safari also asks for it at that address from the Fly pages, which don't declare one (next.config.ts).
  icons: { apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }] },
  appleWebApp: { title: "Dip scanner" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#161a20" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
