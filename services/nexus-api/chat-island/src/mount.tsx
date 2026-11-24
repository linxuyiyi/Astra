import { createRoot } from "react-dom/client";
import type { MountConversation } from "../../shared/conversation";
import { Conversation } from "./conversation/Conversation";
import "./index.css";

export const mountConversation: MountConversation = (element, props) => {
  const root = createRoot(element);
  root.render(<Conversation {...props} />);
  return () => root.unmount();
};
