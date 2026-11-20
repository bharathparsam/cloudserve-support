/* Generated from docs/contracts.schema.json. Do not edit. */

export type Channel = "email" | "chat" | "docs_comment" | "forum";
export type Body = string;
export type Subject = string;
export type TicketId = string | null;
export type ReceivedAt = string | null;
export type CustomerId = string | null;
export type CustomerName = string | null;

export interface TicketInput {
  channel: Channel;
  body: Body;
  subject?: Subject;
  ticket_id?: TicketId;
  received_at?: ReceivedAt;
  customer_id?: CustomerId;
  customer_name?: CustomerName;
}
