import { ChatScreen } from "@/components/chat/ChatScreen";

export default async function ChatSessionPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = await params;
  return <ChatScreen sessionId={sessionId} />;
}
