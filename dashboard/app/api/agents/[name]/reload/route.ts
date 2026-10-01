import { NextRequest, NextResponse } from "next/server";
import { agentFetch, isAuthenticated, unauthorizedResponse } from "@/lib/api";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  try {
    const response = await agentFetch(`/agents/${name}/reload`, {
      method: "POST",
    });

    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).error || detail;
      } catch {}
      return NextResponse.json(
        { error: `Failed to reload agent: ${detail}` },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to reload agent" },
      { status: 500 }
    );
  }
}
