export interface ConversationBase {
    title: string;
    status: string;
}

export interface ConversationCreate extends ConversationBase {}

export interface ConversationResponse extends ConversationBase {
    id: string;
    user_id: string;
    opencode_session_id?: string;
    last_message_at: string;
    created_at: string;
    updated_at: string;
}

export interface MessagePartResponse {
    id: string;
    message_id: string;
    part_type: string;
    seq: number;
    payload_json: any;
    created_at: string;
}

export interface MessageBase {
    role: string;
    message_type: string;
    content: string;
    status: string;
}

export interface MessageCreate {
    content: string;
    page_context?: any;
}

export interface MessageResponse extends MessageBase {
    id: string;
    conversation_id: string;
    opencode_message_id?: string;
    parts: MessagePartResponse[];
    created_at: string;
    updated_at: string;
}

export interface OperationResponse {
    id: string;
    operation_id: string;
    conversation_id: string;
    message_id?: string;
    user_id: string;
    operation_type: string;
    risk_level: string;
    status: string;
    request_json: any;
    preview_json: any;
    result_json: any;
    expires_at?: string;
    confirmed_at?: string;
    executed_at?: string;
    created_at: string;
    updated_at: string;
}

export interface AssistantEvent {
    event: string;
    conversation_id?: string;
    message_id?: string;
    timestamp: string;
    data: any;
}
