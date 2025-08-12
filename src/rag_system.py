import logging
import time
from typing import Dict, Any, List
import json

from config import Config
from data_loader import EcommerceDataLoader
from meilisearch_client import MeilisearchClient
from openrouter_client import OpenRouterClient

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format='%(message)s' 
)
logger = logging.getLogger(__name__)

logging.getLogger('meilisearch_client').setLevel(logging.WARNING)
logging.getLogger('data_loader').setLevel(logging.WARNING)
logging.getLogger('openrouter_client').setLevel(logging.WARNING)

class AgenticRAGSystem:
    """Main RAG system for e-commerce data analysis"""
    
    def __init__(self):
        """Initialize the RAG system"""
        self.data_loader = EcommerceDataLoader()
        self.meilisearch_client = MeilisearchClient()
        self.openrouter_client = OpenRouterClient()
        
        Config.validate_config()
    
    def _smart_search(self, query: str, max_results: int, filters: str = None) -> Dict[str, Any]:
        """Perform smart search with improved relevance"""
        try:
            query_lower = query.lower()
            
            results = self.meilisearch_client.search(query, max_results, filters)
            logger.info(f"Initial search found: {len(results.get('hits', []))} results")
            
            if not results.get('hits') or len(results['hits']) < max_results:
                logger.info("Insufficient results, doing category-based search...")
                
                category_mapping = {
                    'clothing': ['clothing', 'clothes', 'dress', 'shirt', 'trousers', 'saree', 'stole', 'kurti', 'hankerchief', 't-shirt', 'shirt', 'gift', 'family', 'personal'],
                    'furniture': ['furniture', 'chair', 'chairs', 'bookcase', 'bookcases', 'table', 'desk', 'home office', 'office', 'home'],
                    'electronics': ['electronics', 'electronic', 'phone', 'phones', 'printer', 'printers', 'game', 'games', 'affordable electronics', 'tech', 'gadget']
                }
                
                matching_categories = []
                for category, keywords in category_mapping.items():
                    if any(keyword in query_lower for keyword in keywords):
                        matching_categories.append(category)
                        logger.info(f"Query matches category: {category}")
                
                if not matching_categories:
                    matching_categories = ['clothing', 'furniture', 'electronics']
                    logger.info("No specific category detected, searching all categories")
                
                all_category_results = []
                for category in matching_categories:
                    logger.info(f"Searching category: {category}")
                    
                    category_filter = f"category = '{category.title()}'"
                    category_results = self.meilisearch_client.search(query, max_results * 2, category_filter)
                    
                    if category_results.get('hits'):
                        logger.info(f"Category {category} found {len(category_results['hits'])} results")
                        all_category_results.extend(category_results['hits'])
                    else:
                        broader_results = self.meilisearch_client.search(category, max_results, category_filter)
                        if broader_results.get('hits'):
                            logger.info(f"Broader search in {category} found {len(broader_results['hits'])} results")
                            all_category_results.extend(broader_results['hits'])
                
                unique_results = []
                seen_ids = set()
                for hit in all_category_results:
                    if hit.get('id') not in seen_ids:
                        unique_results.append(hit)
                        seen_ids.add(hit.get('id'))
                        if len(unique_results) >= max_results:
                            break
                
                if unique_results:
                    results['hits'] = unique_results
                    logger.info(f"Category search completed - found {len(unique_results)} unique results")
                else:
                    logger.warning("Category search found no results")
            
            if not results.get('hits') or len(results['hits']) < max_results:
                logger.info("Doing final fallback search...")
                
                query_words = [word for word in query_lower.split() if len(word) > 2]
                relevant_terms = ['clothing', 'furniture', 'electronics', 'phone', 'chair', 'saree', 'stole', 'affordable', 'gift', 'office']
                
                for term in relevant_terms + query_words:
                    if len(results.get('hits', [])) >= max_results:
                        break
                    
                    term_results = self.meilisearch_client.search(term, max_results - len(results.get('hits', [])))
                    if term_results.get('hits'):
                        if not results.get('hits'):
                            results = term_results
                        else:
                            existing_ids = {hit.get('id') for hit in results['hits']}
                            for hit in term_results['hits']:
                                if hit.get('id') not in existing_ids and len(results['hits']) < max_results:
                                    results['hits'].append(hit)
                
                logger.info(f"Fallback search completed - total results: {len(results.get('hits', []))}")
            
            if not results.get('hits'):
                results['hits'] = []
            if 'estimatedTotalHits' not in results:
                results['estimatedTotalHits'] = len(results['hits'])
            if 'processingTimeMs' not in results:
                results['processingTimeMs'] = 0
                
            logger.info(f"Final search results: {len(results['hits'])} hits")
            return results
            
        except Exception as e:
            logger.error(f"Smart search failed: {e}")
            return {    
                'hits': [],
                'estimatedTotalHits': 0,
                'processingTimeMs': 0
            }
    
    def _detect_personal_context(self, query: str) -> bool:
        """Automatically detect if the query is for personal shopping context"""
        query_lower = query.lower()
        
        personal_keywords = [
            'shopping', 'buy', 'buying', 'purchase', 'purchasing',
            'gift', 'gifts', 'present', 'presents', 'souvenir', 'souvenirs',
            'vacation', 'travel', 'trip', 'holiday', 'goa', 'beach',
            'personal', 'family', 'friends', 'myself', 'me',
            'recommend', 'recommendation', 'suggest', 'suggestion',
            'what to buy', 'what should i buy', 'what can i take',
            'need', 'want', 'looking for', 'searching for'
        ]
        
        business_keywords = [
            'business', 'profit', 'profitability', 'revenue', 'loss',
            'margin', 'margins', 'analysis', 'analytics', 'performance',
            'inventory', 'stock', 'quarterly', 'annual', 'strategy',
            'management', 'optimization', 'efficiency', 'roi',
            'highest', 'best', 'top', 'most profitable', 'profit margins',
            'financial', 'commercial', 'enterprise', 'corporate'
        ]
        
        personal_matches = sum(1 for keyword in personal_keywords if keyword in query_lower)
        business_matches = sum(1 for keyword in business_keywords if keyword in query_lower)
        
        if business_matches > personal_matches and business_matches > 0:
            return False
        else:
            return True
    
    def setup_index(self) -> bool:
        """Set up the Meilisearch index with data"""
        try:
            print("Initializing system...")
            
            if not self.meilisearch_client.health_check():
                logger.error("Meilisearch health check failed")
                print("Error: Meilisearch is not running. Please start it first.")
                return False

            # Always delete and recreate index for fresh start
            self.meilisearch_client.delete_index()
            
            import time
            time.sleep(2)
            
            self.meilisearch_client.create_index()
            self.meilisearch_client.configure_search_settings()
            
            print("Loading data...")
            documents = self.data_loader.process_data()
            
            logger.info(f"Adding {len(documents)} documents to index")
            self.meilisearch_client.add_documents(documents)
            
            print("System ready!")
            return True
            
        except Exception as e:
            logger.error(f"System initialization failed: {e}")
            print(f"Error: {e}")
            raise
    
    def query(self, 
              user_query: str, 
              max_results: int = None,
              filters: str = None,
              model: str = None) -> Dict[str, Any]:
        """Process a user query using RAG"""
        try:
            start_time = time.time()
            logger.info(f"Processing query: '{user_query}'")
            
            search_results = self._smart_search(user_query, max_results or Config.MAX_SEARCH_RESULTS, filters)
            
            search_time = time.time() - start_time
            
            if not search_results['hits']:
                logger.warning("No relevant documents found for query")
                return {
                    "query": user_query,
                    "answer": "I couldn't find any relevant data to answer your question. Please try rephrasing your query.",
                    "context": [],
                    "search_time": search_time,
                    "llm_time": 0.0,
                    "total_time": time.time() - start_time,
                    "sources": [],
                    "search_stats": {
                        "total_hits": 0,
                        "processing_time_ms": 0
                    }
                }
            
            context_docs = search_results['hits']
            is_personal_context = self._detect_personal_context(user_query)
            messages = self.openrouter_client.create_rag_prompt(user_query, context_docs, is_personal_context)
            
            llm_start_time = time.time()
            llm_response = self.openrouter_client.generate_response(
                messages=messages,
                model=model,
                temperature=0.3,
                max_tokens=800
            )
            
            llm_time = time.time() - llm_start_time
            total_time = time.time() - start_time
            
            answer = llm_response['choices'][0]['message']['content']
            
            sources = []
            for doc in context_docs:
                if is_personal_context:
                    content = doc.get('content', '')
                else:
                    content = doc.get('business_content', doc.get('content', ''))  # Business content
                
                profit_data = None
                if not is_personal_context and doc.get("profit") is not None:
                    profit = doc.get("profit")
                    if profit > 0:
                        profit_data = f"+${profit:.2f}"  # Profit with + sign
                    elif profit < 0:
                        profit_data = f"-${abs(profit):.2f}"  # Loss with - sign
                    else:
                        profit_data = "$0.00"  # Zero profit

                sources.append({
                    "order_id": doc.get("order_id"),
                    "category": doc.get("category"),
                    "sub_category": doc.get("sub_category"),
                    "amount": doc.get("amount"),
                    "profit": profit_data,  # Formatted profit/loss data
                    "content": content
                })
            
            result = {
                "query": user_query,
                "answer": answer,
                "context": context_docs,
                "context_detected": "PERSONAL" if is_personal_context else "BUSINESS",
                "search_time": search_time,
                "llm_time": llm_time,
                "total_time": total_time,
                "sources": sources,
                "search_stats": {
                    "total_hits": search_results.get('estimatedTotalHits', 0),
                    "processing_time_ms": search_results.get('processingTimeMs', 0)
                }
            }
            
            logger.info(f"Query completed in {total_time:.2f}s (search: {search_time:.2f}s, LLM: {llm_time:.2f}s)")
            
            return result
            
        except Exception as e:
            logger.error(f"Error processing query: {e}")
            raise
    
    def get_system_info(self) -> Dict[str, Any]:
        """Get system information and status"""
        try:
            info = {
                "system": "Agentic RAG System",
                "version": "1.0.0",
                "components": {
                    "meilisearch": {
                        "status": "healthy" if self.meilisearch_client.health_check() else "unhealthy",
                        "url": self.meilisearch_client.url
                    },
                    "openrouter": {
                        "status": "healthy" if self.openrouter_client.test_connection() else "unhealthy",
                        "model": Config.LLM_MODEL
                    }
                }
            }
            
            try:
                stats = self.meilisearch_client.get_index_stats()
                info["index_stats"] = stats
            except:
                info["index_stats"] = "Not available"
            
            return info
            
        except Exception as e:
            logger.error(f"Error getting system info: {e}")
            return {"error": str(e)}

if __name__ == "__main__":
    import sys
    
    try:
        rag = AgenticRAGSystem()
        
        if not rag.setup_index():
            print("System initialization failed. Exiting.")
            sys.exit(1)
        
        if len(sys.argv) > 1:
            query = " ".join(sys.argv[1:])
            print(f"\nQuery: {query}")
            
            result = rag.query(query)
            print(f"\n{result['answer']}")
            print(f"Response time: {result['total_time']:.1f}s")
            
        else:
            print("\nE-commerce Data Assistant")
            print("Ask me about orders, products, sales, and more!")
            print("Type 'quit' to exit\n")
            
            while True:
                try:
                    query = input("Query: ").strip()
                    if query.lower() in ['quit', 'exit', 'q']:
                        print("Goodbye!")
                        break
                    if not query:
                        continue
                    
                    result = rag.query(query)
                    print(f"\n{result['answer']}")
                    print(f"Response time: {result['total_time']:.1f}s")
                    print()
                    
                except KeyboardInterrupt:
                    print("\nGoodbye!")
                    break
                except Exception as e:
                    print(f"Error: {e}")
                    
    except Exception as e:
        print(f"System initialization failed: {e}")
        sys.exit(1)