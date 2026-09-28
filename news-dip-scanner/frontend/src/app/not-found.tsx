import type { Metadata } from "next";
import NotFoundView from "@/components/NotFoundView";
import { getMeIfSignedIn } from "@/lib/api";

export const metadata: Metadata = { title: "Not found" };

/** The not-found page of the React pages (an idea that doesn't exist); Fly answers for its own paths. Signed in, it
 * keeps the site's header (getMeIfSignedIn never sends anyone to the sign-in page). */
export default async function NotFound() {
  return <NotFoundView me={await getMeIfSignedIn()} />;
}
