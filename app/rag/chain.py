import os
from dotenv import load_dotenv

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

load_dotenv()


def build_rag_chain(vectorstore):

    # ✅ LLM
    llm = ChatGoogleGenerativeAI(
        model="models/gemini-2.5-flash",
        temperature=0.3,
        api_key=os.getenv("GOOGLE_API_KEY")
    )

    # ✅ FIXED PROMPT (SMART BEHAVIOR)
    prompt = ChatPromptTemplate.from_template(
        """
        You are a smart and helpful AI assistant.

        - Use memory ONLY if it is relevant to the question.
        - If memory is NOT relevant, ignore it and answer normally.
        - Never say "I cannot answer".

        User: {user_name}

        Memory:
        {context}

        Question:
        {question}

        Give a clear and helpful answer in simple English.
        """
    )

    # ✅ CONTEXT RETRIEVAL (USER + GLOBAL)
    def retrieve_context(question, user_id):
        try:
            docs_user = []
            if user_id:
                docs_user = vectorstore.similarity_search(
                    question,
                    k=3,
                    filter={"user_id": int(user_id)}
                )

            docs_global = vectorstore.similarity_search(
                question,
                k=2,
                filter={"user_id": -1}
            )

            all_docs = docs_user + docs_global

            # ✅ FIX: handle empty memory
            if not all_docs:
                return "No relevant memory"

            return "\n".join(doc.page_content for doc in all_docs)

        except Exception as e:
            print(f"🔥 Retrieval Error: {e}")
            return "No relevant memory"

    # ✅ RAG CHAIN
    rag_chain = (
        {
            "context": RunnableLambda(lambda x: retrieve_context(x["question"], x.get("user_id"))),
            "question": RunnableLambda(lambda x: x["question"]),
            "user_name": RunnableLambda(lambda x: x["user_name"])
        }
        | prompt
        | llm
    )

    return rag_chain